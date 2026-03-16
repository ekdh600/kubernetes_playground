"""
main.py — Kubernetes Playground Platform 게이트웨이 API

역할:
- 사용자 요청을 받아 플레이그라운드(K8s 파드 기반 실습 환경)를 생성/삭제한다.
- cluster_registry / k8s_manager를 통해 멀티 클러스터에 리소스를 배포한다.
- xterm.js 브라우저 터미널과 Kubernetes Pod를 WebSocket + kubernetes.stream으로 연결한다.
- 백그라운드 루프에서 60초마다 만료된 플레이그라운드와 세션 ConfigMap을 정리한다.

[주의] 현재 코드에서 아직 남아 있는 개선 필요 항목:
- /clusters 엔드포인트는 인증 없이 클러스터 목록을 반환한다(필드 최소화로 완화 가능).
- create_playground()의 admin 권한 인라인 파싱은 auth.py 로직과 분리되어 있다.
- 미사용 import: StaticFiles, FileResponse, JSONResponse (제거 예정).
"""

import uuid
import time
import os
import asyncio
import uvicorn  # if __name__ == "__main__" 블록에서 직접 실행 시 사용
import base64
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    Depends,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel
from kubernetes.stream import stream
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from key_manager import generate_ssh_key_pair
from k8s_manager import K8sManager
from auth import (
    verify_admin,
    check_admin_credentials,
    verify_admin_session,
    create_admin_session,
    delete_admin_session,
)
from session_manager import (
    get_or_create_session_id,
    get_active_playground,
    bind_playground,
    clear_playground,
    clear_all_sessions_for_playground,
    cleanup_expired_sessions,
)
from cluster_registry import (
    register_cluster,
    list_clusters,
    get_cluster,
    delete_cluster,
    get_cached_manager,
    invalidate_cache,
)

# ── 환경 변수 ─────────────────────────────────────────────────
# 플레이그라운드 Pod/Service가 배포될 네임스페이스. 기본값 "study".
NAMESPACE = os.getenv("NAMESPACE", "study")

# IP 기반 요청 속도 제한기. 악용(무한 프로비저닝)을 방지한다.
limiter = Limiter(key_func=get_remote_address)

# 단기 WebSocket 티켓 저장소 (임시 캐시, UUID -> {"private_key": key, "cluster_id": cid, "expires": float})
WS_TICKETS = {}


# ── 서버 수명주기 (Lifespan) ──────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 앱의 시작/종료 수명주기를 관리한다.
    서버 시작 시 백그라운드 정리 루프를 asyncio Task로 등록하고,
    서버 종료 시 해당 Task를 취소한다.
    """
    print("Starting background cleanup loop...")

    async def cleanup_loop():
        """
        60초 간격으로 실행되는 백그라운드 정리 루프.
        - 등록된 모든 클러스터에서 만료된 플레이그라운드를 삭제한다.
        - 만료된 세션 ConfigMap도 함께 정리한다.
        각 클러스터의 오류는 독립적으로 처리하여 한 클러스터의 장애가
        다른 클러스터의 정리 작업을 막지 않도록 한다.
        """
        while True:
            try:
                deleted_total = 0
                for cluster in list_clusters():
                    try:
                        mgr = get_cached_manager(cluster["id"])
                        # 생성 후 86400초(24시간)가 지난 플레이그라운드를 삭제
                        deleted_total += mgr.cleanup_expired_playgrounds(NAMESPACE)
                    except Exception as ce:
                        print(f"Cleanup error for cluster {cluster['name']}: {ce}")

                if deleted_total > 0:
                    print(
                        f"Sweep complete: deleted {deleted_total} expired playgrounds."
                    )

                # 만료된 세션 ConfigMap 정리
                try:
                    cleanup_expired_sessions()
                except Exception as cse:
                    print(f"Session cleanup error: {cse}")

            except Exception as e:
                print(f"Cleanup loop error: {e}")

            await asyncio.sleep(60)

    task = asyncio.create_task(cleanup_loop())
    yield  # 서버가 요청을 처리하는 동안 여기서 대기
    task.cancel()
    print("Shutting down...")


# ── FastAPI 앱 초기화 ──────────────────────────────────────────
app = FastAPI(title="Kubernetes Playground Platform", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── 요청/응답 모델 ─────────────────────────────────────────────
class PlaygroundRequest(BaseModel):
    """일반 사용자가 플레이그라운드를 생성할 때 전달하는 요청 본문."""

    cluster_id: str  # 플레이그라운드를 배포할 클러스터 ID


class PlaygroundResponse(BaseModel):
    """플레이그라운드 생성 성공 시 반환되는 응답. private_key는 이 응답에서만 제공된다."""

    id: str
    user: str  # SSH 접속 계정 ("ubuntu" 고정)
    host: str  # 파드가 실행 중인 노드의 IP
    port: int  # SSH NodePort 번호
    private_key: str  # RSA 개인키 (PEM 형식). 이 응답 이후에는 재발급 불가
    cluster_id: str
    message: str


class AdminPlaygroundCreateRequest(BaseModel):
    """관리자가 커스텀 RBAC 설정으로 플레이그라운드를 생성할 때 사용하는 요청 본문."""

    cluster_id: str
    namespaces: list[str] = ["*"]  # "*" = 클러스터 전체, "sandbox" = 전용 네임스페이스
    verbs: list[str] = ["*"]  # 기본적으로 모든 권한 부여


class BulkRBACUpdateRequest(BaseModel):
    """일괄 RBAC 업데이트 요청 본문."""

    playground_ids: list[str]
    cluster_id: str
    namespaces: list[str]
    verbs: list[str]


class BulkDeleteRequest(BaseModel):
    """일괄 삭제 요청 본문. 각 클러스터별로 삭제할 playground_id 목록을 받는다."""

    targets: dict[str, list[str]]


# ── 플레이그라운드 API ─────────────────────────────────────────


@app.post("/playground", response_model=PlaygroundResponse)
@limiter.limit("5/minute")
def create_playground(
    request: Request,
    response: Response,
    body: PlaygroundRequest,
):
    """
    플레이그라운드를 생성한다. 세션 쿠키로 사용자를 식별하며,
    세션당 1개의 플레이그라운드만 허용한다(관리자는 이 제한을 우회한다).

    리소스 생성 순서가 중요하다:
    1. SSH 키 Secret → 2. Sandbox 네임스페이스 → 3. RBAC → 4. SA 토큰 →
    5. kubeconfig Secret → 6. Deployment → 7. Service → 8. 세션 바인딩

    생성 중 오류 발생 시 이미 만들어진 리소스를 즉시 롤백한다.
    """
    session_id = get_or_create_session_id(request, response)

    # 관리자 여부를 Authorization 헤더로 판단한다.
    # 관리자는 1인 1플레이그라운드 제한을 우회할 수 있다.
    # [개선 필요] auth.py의 verify_admin Depends를 Optional로 재사용하는 것이 더 안전하다.
    is_admin = False
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            auth_decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = auth_decoded.split(":", 1)
            is_admin = check_admin_credentials(username, password)
        except Exception:
            pass  # 잘못된 헤더는 조용히 무시하고 일반 사용자로 처리

    # 세션에 이미 활성 플레이그라운드가 있으면 409를 반환한다.
    # 프론트엔드는 이 응답을 받으면 기존 플레이그라운드로 재연결한다.
    existing = get_active_playground(session_id)
    if existing and not is_admin:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_exists",
                "playground_id": existing["playground_id"],
                "cluster_id": existing["cluster_id"],
                "message": "You already have an active playground. Delete it first.",
            },
        )

    cluster_id = body.cluster_id
    cluster_info = get_cluster(cluster_id)
    if not cluster_info:
        raise HTTPException(status_code=404, detail=f"Cluster {cluster_id} not found")
    mgr = get_cached_manager(cluster_id)

    # playground_id는 UUID의 앞 8자리로 충분히 고유하면서 짧은 식별자를 만든다.
    playground_id = str(uuid.uuid4())[:8]
    private_key, public_key = generate_ssh_key_pair()
    sandbox_ns = f"sandbox-{playground_id}"

    try:
        print(f"Provisioning Playground {playground_id} on cluster {cluster_id}...")

        # 네임스페이스가 없으면 생성, 있으면(409) 그냥 진행한다.
        try:
            mgr.setup_namespace(NAMESPACE)
        except Exception as ns_err:
            print(f"Namespace setup warning ({cluster_id}): {ns_err}")

        # SSH 공개키(authorized_keys)와 개인키를 Secret으로 저장.
        # 개인키는 관리자가 나중에 /admin/playgrounds/{id}/ssh 로 재조회할 수 있다.
        mgr.create_secret(NAMESPACE, playground_id, public_key, private_key=private_key)

        # 사용자 전용 격리 네임스페이스 생성 (sandbox-{id})
        mgr.create_sandbox_namespace(sandbox_ns, playground_id)

        # sandbox 네임스페이스에 admin-sa + ClusterRole "admin" RoleBinding 생성
        sa_name = mgr.setup_sandbox_rbac(sandbox_ns)

        # Legacy SA Token Secret 방식으로 토큰을 발급받는다.
        # (TokenRequest API 방식보다 호환성이 높음)
        token = mgr.get_service_account_token(sandbox_ns, sa_name)

        # 파드 내에서 kubectl이 사용할 kubeconfig를 Secret으로 생성한다.
        kubeconfig_secret = mgr.create_kubeconfig_secret(
            NAMESPACE, playground_id, sandbox_ns, token
        )

        # SSH 서버 파드(playground-runner 이미지) 배포
        mgr.create_deployment(
            NAMESPACE, playground_id, f"ssh-key-{playground_id}", kubeconfig_secret
        )

        # NodePort SSH 서비스 생성 (포트는 Kubernetes가 자동 할당)
        svc_name = mgr.create_service(NAMESPACE, playground_id)
        node_port = mgr.get_service_node_port(NAMESPACE, svc_name)
        host_ip = mgr.get_pod_node_ip(NAMESPACE, playground_id)

        # 세션 ConfigMap에 playground_id와 cluster_id를 기록한다.
        bind_playground(session_id, playground_id, cluster_id)

        return PlaygroundResponse(
            id=playground_id,
            user="ubuntu",
            host=host_ip,
            port=node_port,
            private_key=private_key,
            cluster_id=cluster_id,
            message="Playground created successfully.",
        )
    except Exception as e:
        # 부분적으로 생성된 리소스를 모두 정리하여 클러스터를 깨끗한 상태로 유지한다.
        # delete_playground()가 study 리소스와 sandbox 네임스페이스를 모두 정리한다.
        mgr.delete_playground(NAMESPACE, playground_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/playground/{playground_id}")
def delete_playground(request: Request, response: Response, playground_id: str):
    """
    사용자가 자신의 플레이그라운드를 삭제한다.
    세션 쿠키로 소유권을 검증하며, 다른 사람의 플레이그라운드는 삭제할 수 없다.
    """
    session_id = get_or_create_session_id(request, response)
    active = get_active_playground(session_id)

    # 세션에 바인딩된 playground_id와 요청한 playground_id가 일치하는지 확인
    if not active or active.get("playground_id") != playground_id:
        raise HTTPException(
            status_code=403, detail="Forbidden: You do not own this playground."
        )

    cluster_id = active["cluster_id"]
    try:
        mgr = get_cached_manager(cluster_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Cluster not found")

    try:
        mgr.delete_playground(NAMESPACE, playground_id)
        # 이 playground_id를 가진 모든 세션 ConfigMap의 바인딩을 해제한다.
        clear_all_sessions_for_playground(playground_id)
        return {"message": f"Playground {playground_id} deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/playground/me")
@limiter.limit("30/minute")
def my_playground(request: Request, response: Response):
    """
    현재 세션의 활성 플레이그라운드 정보를 반환한다.
    페이지 새로고침 후 기존 플레이그라운드를 복원할 때 사용한다.
    SSH 접속 정보(host/port)는 파드가 아직 기동 중이면 응답에 포함되지 않을 수 있다.
    """
    session_id = get_or_create_session_id(request, response)
    active = get_active_playground(session_id)
    if not active:
        return {}

    playground_id = active["playground_id"]
    cluster_id = active.get("cluster_id")

    # cluster_id가 없거나 레거시 "default" 값이면 클러스터를 찾을 수 없으므로 빈 응답
    if not cluster_id or cluster_id == "default":
        return {}

    try:
        mgr = get_cached_manager(cluster_id)
    except Exception:
        return {}

    # SSH 정보 조회는 best-effort — 파드가 아직 스케줄링 중이면 예외가 발생하며 무시된다.
    try:
        svc_name = f"ubuntu-sshd-svc-{playground_id}"
        node_port = mgr.get_service_node_port(NAMESPACE, svc_name)
        host_ip = mgr.get_pod_node_ip(NAMESPACE, playground_id)
        active["host"] = host_ip
        active["port"] = node_port
        active["user"] = "ubuntu"
        active["ssh_command"] = (
            f"ssh ubuntu@{host_ip} -p {node_port} -i playground_key.pem"
        )
    except Exception:
        pass

    return active


@app.get("/clusters")
@limiter.limit("30/minute")
def public_list_clusters(request: Request, response: Response):
    """
    일반 사용자가 플레이그라운드를 생성할 때 클러스터를 선택하기 위한 목록을 반환한다.
    [보안 강화] session_id 쿠키가 있는 사용자만 접근 가능하도록 제한한다.
    """
    # 세션 확인 (없으면 발급)
    get_or_create_session_id(request, response)

    clusters = list_clusters()
    return [{"id": c["id"], "name": c["name"]} for c in clusters]


# ── 관리자 세션 API (Login/Logout) ──────────────────────────


@app.post("/admin/login")
async def admin_login(request: Request, response: Response):
    """
    관리자 로그인을 처리하고 세션 쿠키를 발급한다.
    HTTP Basic Auth 또는 JSON Body로 전달된 자격증명을 검증한다.
    """
    # 1. 자격증명 추출 (Basic Auth 또는 JSON)
    username = None
    password = None

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception:
            pass

    if not username:
        try:
            body = await request.json()
            username = body.get("username")
            password = body.get("password")
        except Exception:
            pass

    if not username or not password or not check_admin_credentials(username, password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect admin username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    # 2. 세션 생성
    session_id = create_admin_session(username)

    # 3. 쿠키 설정 (HttpOnly, Secure, SameSite)
    cookie_secure = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    response.set_cookie(
        key="admin_session",
        value=session_id,
        httponly=True,  # JS 접근 차단 (XSS 방지)
        samesite="strict",  # CSRF 방지
        secure=cookie_secure,  # HTTPS 환경이라면 필수
        max_age=43200,  # 12시간
    )
    return {"message": "Login successful", "username": username}


@app.post("/admin/logout")
async def admin_logout(request: Request, response: Response):
    """관리자 세션을 파기하고 쿠키를 삭제한다."""
    session_id = request.cookies.get("admin_session")
    if session_id:
        delete_admin_session(session_id)
    response.delete_cookie("admin_session")
    return {"message": "Logged out"}


# ── 관리자 클러스터 API ───────────────────────────────────────


@app.get("/admin/clusters")
def admin_list_clusters(admin: str = Depends(verify_admin_session)):
    """등록된 모든 클러스터 목록을 반환한다 (관리자 전용)."""
    return list_clusters()


@app.post("/admin/clusters")
async def admin_register_cluster(
    request: Request, admin: str = Depends(verify_admin_session)
):
    """
    새 클러스터를 등록한다. kubeconfig YAML 전체를 받아 K8s Secret으로 저장한다.
    저장 전 YAML 유효성 검증은 현재 미구현 — 잘못된 kubeconfig는 사용 시점에 오류 발생.
    """
    body = await request.json()
    name = body.get("name", "").strip()
    kubeconfig_yaml = body.get("kubeconfig", "").strip()
    if not name or not kubeconfig_yaml:
        raise HTTPException(status_code=400, detail="name and kubeconfig are required")
    try:
        cluster_id = register_cluster(name, kubeconfig_yaml)
        return {
            "id": cluster_id,
            "name": name,
            "message": "Cluster registered successfully.",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/admin/clusters/{cluster_id}")
def admin_delete_cluster(cluster_id: str, admin: str = Depends(verify_admin_session)):
    """클러스터 등록을 해제한다. 인메모리 캐시도 함께 무효화한다."""
    cluster = get_cluster(cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    delete_cluster(cluster_id)
    invalidate_cache(cluster_id)
    return {"message": f"Cluster {cluster_id} deleted."}


@app.get("/admin/clusters/{cluster_id}/namespaces")
def admin_list_cluster_namespaces(
    cluster_id: str, admin: str = Depends(verify_admin_session)
):
    """
    특정 클러스터의 전체 네임스페이스 목록을 반환한다.
    관리자가 커스텀 플레이그라운드 생성 시 네임스페이스를 선택하는 UI에서 사용한다.
    """
    cluster_info = get_cluster(cluster_id)
    if not cluster_info:
        raise HTTPException(status_code=404, detail=f"Cluster {cluster_id} not found")
    mgr = get_cached_manager(cluster_id)
    try:
        namespaces = mgr.list_namespaces()
        return {"namespaces": namespaces}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 관리자 플레이그라운드 API ─────────────────────────────────


@app.post("/admin/playgrounds", response_model=PlaygroundResponse)
async def admin_create_playground(
    request: Request,
    response: Response,
    body: AdminPlaygroundCreateRequest,
    admin: str = Depends(verify_admin_session),
):
    """
    관리자가 커스텀 RBAC 권한으로 플레이그라운드를 생성한다.
    namespaces=["*"]이면 클러스터 전체에 대한 ClusterRoleBinding을 생성하므로
    verbs 범위에 주의해야 한다(write 동사 부여 시 클러스터 전체 장악 가능).
    일반 create_playground와 달리 세션당 1개 제한이 없다.
    """
    session_id = get_or_create_session_id(request, response)
    cluster_id = body.cluster_id
    cluster_info = get_cluster(cluster_id)
    if not cluster_info:
        raise HTTPException(status_code=404, detail=f"Cluster {cluster_id} not found")
    mgr = get_cached_manager(cluster_id)

    playground_id = str(uuid.uuid4())[:8]
    private_key, public_key = generate_ssh_key_pair()
    sandbox_ns = f"sandbox-{playground_id}"

    try:
        print(
            f"Provisioning Admin Custom Playground {playground_id} on cluster {cluster_id}..."
        )

        try:
            mgr.setup_namespace(NAMESPACE)
        except Exception as ns_err:
            print(f"Namespace setup warning ({cluster_id}): {ns_err}")

        mgr.create_secret(NAMESPACE, playground_id, public_key, private_key=private_key)
        mgr.create_sandbox_namespace(sandbox_ns, playground_id)

        # 커스텀 RBAC: 관리자가 지정한 namespaces와 verbs로 SA 권한을 설정한다.
        sa_name = mgr.setup_custom_rbac(
            sandbox_ns, body.namespaces, body.verbs, playground_id
        )

        token = mgr.get_service_account_token(sandbox_ns, sa_name)
        kubeconfig_secret = mgr.create_kubeconfig_secret(
            NAMESPACE, playground_id, sandbox_ns, token
        )
        mgr.create_deployment(
            NAMESPACE, playground_id, f"ssh-key-{playground_id}", kubeconfig_secret
        )

        # RBAC 설정(허용 네임스페이스/동사)을 서비스 어노테이션에 저장해 목록 조회 시 표시한다.
        svc_annotations = {
            "playground.namespaces": ",".join(body.namespaces),
            "playground.verbs": ",".join(body.verbs),
        }
        svc_name = mgr.create_service(
            NAMESPACE, playground_id, annotations=svc_annotations
        )
        node_port = mgr.get_service_node_port(NAMESPACE, svc_name)
        host_ip = mgr.get_pod_node_ip(NAMESPACE, playground_id)

        return PlaygroundResponse(
            id=playground_id,
            user="ubuntu",
            host=host_ip,
            port=node_port,
            private_key=private_key,
            cluster_id=cluster_id,
            message="Custom Admin Playground created successfully.",
        )
    except Exception as e:
        # delete_playground()가 study 리소스와 sandbox 네임스페이스를 모두 정리한다.
        mgr.delete_playground(NAMESPACE, playground_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/playgrounds")
def admin_list_all(admin: str = Depends(verify_admin_session)):
    """
    등록된 모든 클러스터에서 실행 중인 플레이그라운드 목록을 반환한다.
    각 클러스터의 조회 오류는 에러 항목으로 포함하고 다른 클러스터 조회를 계속한다.
    """
    result = []
    for cluster in list_clusters():
        try:
            mgr = get_cached_manager(cluster["id"])
            items = mgr.list_playgrounds(NAMESPACE)
            for item in items:
                item["cluster"] = cluster["name"]
                item["cluster_id"] = cluster["id"]
            result.extend(items)
        except Exception as e:
            result.append({"error": str(e), "cluster": cluster["name"]})
    return result


@app.delete("/admin/playgrounds/{playground_id}")
def admin_delete_playground(
    playground_id: str,
    cluster_id: str,
    admin: str = Depends(verify_admin_session),
):
    """
    관리자가 강제로 플레이그라운드를 삭제한다.
    소유자 여부와 관계없이 삭제 가능하다.
    """
    try:
        mgr = get_cached_manager(cluster_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Cluster not found")
    try:
        mgr.delete_playground(NAMESPACE, playground_id)
        clear_all_sessions_for_playground(playground_id)
        return {"message": f"Playground {playground_id} deleted by admin."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/admin/playgrounds/{playground_id}/rbac")
def admin_update_playground_rbac(
    playground_id: str,
    body: AdminPlaygroundCreateRequest,
    admin: str = Depends(verify_admin_session),
):
    """
    실행 중인 플레이그라운드의 RBAC 권한을 동적으로 변경한다.
    파드를 재시작하지 않고 Role/ClusterRole 바인딩만 교체한다.
    kubeconfig의 ServiceAccount 토큰은 그대로 유지되므로 즉시 반영된다.
    """
    cluster_id = body.cluster_id
    try:
        mgr = get_cached_manager(cluster_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Cluster not found")
    sandbox_ns = f"sandbox-{playground_id}"

    try:
        mgr.update_custom_rbac(
            NAMESPACE, playground_id, sandbox_ns, body.namespaces, body.verbs
        )
        return {"message": "RBAC updated successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/playgrounds/bulk-update-rbac")
def admin_bulk_update_playground_rbac(
    body: BulkRBACUpdateRequest,
    admin: str = Depends(verify_admin),
):
    """
    여러 플레이그라운드의 RBAC 권한을 일괄 변경한다.
    모든 플레이그라운드가 동일한 클러스터에 있다고 가정한다(프론트엔드에서 필터링).
    """
    cluster_id = body.cluster_id
    try:
        mgr = get_cached_manager(cluster_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Cluster not found")

    results = []
    errors = []
    for pg_id in body.playground_ids:
        try:
            sandbox_ns = f"sandbox-{pg_id}"
            mgr.update_custom_rbac(
                NAMESPACE, pg_id, sandbox_ns, body.namespaces, body.verbs
            )
            results.append(pg_id)
        except Exception as e:
            errors.append({"id": pg_id, "error": str(e)})

    return {"message": "Bulk update completed.", "updated": results, "errors": errors}


@app.post("/admin/playgrounds/bulk-delete")
def admin_bulk_delete_playgrounds(
    body: BulkDeleteRequest,
    admin: str = Depends(verify_admin),
):
    """
    여러 플레이그라운드를 일괄 삭제한다.
    클러스터별로 그룹화된 요청을 처리한다.
    """
    results = []
    errors = []
    for cluster_id, pg_ids in body.targets.items():
        try:
            mgr = get_cached_manager(cluster_id)
        except Exception:
            for pg_id in pg_ids:
                errors.append(
                    {
                        "id": pg_id,
                        "cluster_id": cluster_id,
                        "error": "Cluster not found",
                    }
                )
            continue

        for pg_id in pg_ids:
            try:
                # study 네임스페이스의 리소스와 sandbox 네임스페이스를 일괄 삭제한다.
                mgr.delete_playground(NAMESPACE, pg_id)
                clear_all_sessions_for_playground(pg_id)
                results.append(pg_id)
            except Exception as e:
                errors.append({"id": pg_id, "cluster_id": cluster_id, "error": str(e)})

    return {"message": "Bulk deletion completed.", "deleted": results, "errors": errors}


@app.get("/admin/playgrounds/{playground_id}/ssh")
def admin_playground_ssh_info(
    playground_id: str,
    cluster_id: str,
    admin: str = Depends(verify_admin),
):
    """
    관리자용 SSH 접속 정보 조회 엔드포인트.
    K8s Secret에 보관된 개인키를 복원하여 반환한다.
    admin.html의 [Terminal] 버튼 클릭 시 호출되며, 반환된 private_key는
    프론트엔드에서 localStorage에 임시 저장된 뒤 index.html이 수거한다.
    [보안 주의] private_key가 localStorage에 저장되므로 sessionStorage 사용을 권장한다.
    """
    try:
        mgr = get_cached_manager(cluster_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Cluster not found")
    try:
        svc_name = f"ubuntu-sshd-svc-{playground_id}"
        node_port = mgr.get_service_node_port(NAMESPACE, svc_name)
        host_ip = mgr.get_pod_node_ip(NAMESPACE, playground_id)
        private_key = mgr.get_private_key(NAMESPACE, playground_id)
        return {
            "host": host_ip,
            "port": node_port,
            "user": "ubuntu",
            "ssh_command": f"ssh ubuntu@{host_ip} -p {node_port} -i playground_key.pem",
            "private_key": private_key,
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── WebSocket 터미널 ──────────────────────────────────────────


@app.post("/admin/playgrounds/{playground_id}/ws-ticket")
def create_ws_ticket(
    playground_id: str,
    cluster_id: str,
    admin: str = Depends(verify_admin),
):
    """
    WebSocket 연결을 위한 일회성 단기 티켓(Ticket)을 발급한다.
    URL 쿼리 파라미터로 개인키가 보안성 없이 직접 노출되는 것을 방지하기 위해 사용된다.
    """
    try:
        mgr = get_cached_manager(cluster_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Cluster not found")

    try:
        private_key = mgr.get_private_key(NAMESPACE, playground_id)
        if not private_key:
            raise Exception("Private key not found")

        ticket_id = str(uuid.uuid4())
        WS_TICKETS[ticket_id] = {
            "private_key": private_key,
            "cluster_id": cluster_id,
            "expires": time.time() + 30,  # 30초 후 만료
        }

        # 주기적인 만료 티켓 청소 (호출 시마다 정리)
        expired = [k for k, v in WS_TICKETS.items() if v["expires"] < time.time()]
        for k in expired:
            del WS_TICKETS[k]

        return {"ticket": ticket_id}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.websocket("/ws/{playground_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    playground_id: str,
    cluster_id: str,
    ticket: str = None,  # 관리자 접속용 단기 티켓 (One-time use)
):
    """
    브라우저의 xterm.js 터미널과 Kubernetes Pod를 양방향으로 연결한다.

    소유권 검증 방식:
    1. 세션 쿠키(session_id)로 플레이그라운드 소유자 확인 (일반 사용자)
    2. ticket 대조 (관리자가 admin 대시보드에서 Terminal 버튼 클릭 시 발급받은 단기 난수)

    동작 흐름:
    - Pod가 Running+Ready 상태가 될 때까지 최대 120초 대기하며 진행 상황을 클라이언트에 전송
    - `su - ubuntu` 명령으로 로그인 셸을 열어 .bashrc 로드 및 kubectl 자동완성 활성화
    - k8s_to_ws: 별도 스레드(asyncio.to_thread)에서 Pod stdout/stderr를 폴링하여 WebSocket 전송
    - ws_to_k8s: 브라우저 키 입력을 Pod stdin으로 전달, 1시간 비활성 타임아웃 적용

    [설계 이유] kubernetes.stream이 동기 블로킹 API이므로 to_thread로 격리하고,
    asyncio.run_coroutine_threadsafe로 이벤트 루프에 안전하게 전달한다.
    """
    await websocket.accept()

    # ─ 1단계: 클러스터 매니저 확인 ─
    try:
        mgr = get_cached_manager(cluster_id)
    except Exception as e:
        await websocket.send_text(f"\r\n*** Error: {e} ***\r\n")
        await websocket.close(code=1011)
        return

    # ─ 2단계: 소유권 검증 ─
    session_id = websocket.cookies.get("session_id")
    active = get_active_playground(session_id) if session_id else None
    is_owner = active and active.get("playground_id") == playground_id
    is_authorized = is_owner

    if not is_owner and ticket:
        # 단기 티켓 검증
        ticket_data = WS_TICKETS.get(ticket)
        if ticket_data and ticket_data["expires"] >= time.time():
            # 사용된 티켓 즉시 폐기 (One-time use)
            del WS_TICKETS[ticket]

            if ticket_data["cluster_id"] == cluster_id:
                try:
                    stored_key = mgr.get_private_key(NAMESPACE, playground_id)
                    if stored_key and ticket_data["private_key"] == stored_key:
                        is_authorized = True
                except Exception:
                    pass

    if not is_authorized:
        has_session = "Yes" if session_id else "No"
        await websocket.send_text(
            f"\r\n*** Forbidden: You do not own this playground. "
            f"(Cookie provided: {has_session}) ***\r\n"
        )
        await websocket.close(code=1008)
        return

    # ─ 3단계: Pod 이름 조회 ─
    try:
        pods = mgr.core_v1.list_namespaced_pod(
            namespace=NAMESPACE, label_selector=f"instance={playground_id}"
        )
        if not pods.items:
            await websocket.send_text("\r\n*** Pod not found ***\r\n")
            await websocket.close(code=1000)
            return
        pod_name = pods.items[0].metadata.name
    except Exception as e:
        print(f"Error finding pod: {e}")
        await websocket.close(code=1011)
        return

    # ─ 4단계: Pod Ready 대기 (최대 120초) ─
    print(f"Waiting for pod {pod_name} to be Running...")
    for i in range(120):
        try:
            pod_info = mgr.core_v1.read_namespaced_pod(pod_name, NAMESPACE)
            phase = pod_info.status.phase
            container_statuses = pod_info.status.container_statuses or []
            # 모든 컨테이너가 Ready 상태여야 접속을 시도한다.
            all_ready = (
                all(cs.ready for cs in container_statuses)
                if container_statuses
                else False
            )
            if phase == "Running" and all_ready:
                print(f"Pod {pod_name} is Running and Ready after {i}s.")
                break
            await websocket.send_text(f"\r🔄 Playground is initializing... ({i+1}s)")
        except Exception as e:
            print(f"Error polling pod: {e}")
        await asyncio.sleep(1)
    else:
        # for 루프가 break 없이 끝난 경우 → 120초 초과
        await websocket.send_text("\r\n*** Initialization timed out ***\r\n")
        await websocket.close(code=1000)
        return

    await websocket.send_text("\r\n\r\n*** Connected to Playground ***\r\n\r\n")

    # ─ 5단계: Pod exec 연결 및 양방향 스트리밍 ─
    try:
        # `sudo -i -u ubuntu`: 로그인 셸 환경으로 전환하여 .bashrc를 로드한다.
        # `su` 대신 `sudo`를 사용하는 이유는 bash 터미널 창 크기 조절 시그널(SIGWINCH)을
        # 자식 프로세스로 안전하게 전파하기 위함이다 (su는 프록시하지 않음).
        resp = stream(
            mgr.core_v1.connect_get_namespaced_pod_exec,
            name=pod_name,
            namespace=NAMESPACE,
            container="ubuntu",
            command=["sudo", "-i", "-u", "ubuntu"],
            stderr=True,
            stdin=True,
            stdout=True,
            tty=True,
            _preload_content=False,  # 스트리밍 모드로 연결(블로킹 방지)
        )

        # asyncio.get_running_loop(): 현재 실행 중인 이벤트 루프 참조.
        # to_thread 내부에서 coroutine을 스케줄링할 때 필요하다.
        loop = asyncio.get_running_loop()

        async def k8s_to_ws():
            """
            Pod stdout/stderr → WebSocket 방향 스트리밍.
            kubernetes.stream.update()가 동기 블로킹이므로 asyncio.to_thread로 별도 스레드에서 실행.
            스레드 내에서 WebSocket 코루틴을 직접 호출할 수 없으므로
            run_coroutine_threadsafe로 이벤트 루프에 안전하게 위임한다.
            """
            try:

                def read_stream():
                    while resp.is_open():
                        resp.update(timeout=0.1)
                        if resp.peek_stdout():
                            data = resp.read_stdout()
                            asyncio.run_coroutine_threadsafe(
                                websocket.send_text(data), loop
                            )
                        if resp.peek_stderr():
                            data = resp.read_stderr()
                            asyncio.run_coroutine_threadsafe(
                                websocket.send_text(data), loop
                            )

                await asyncio.to_thread(read_stream)
            except Exception as e:
                print(f"K8s -> WS Error: {e}")
            finally:
                try:
                    await websocket.close()
                except Exception:
                    pass

        async def ws_to_k8s():
            """
            WebSocket(브라우저 키 입력) → Pod stdin 방향 스트리밍.
            1시간 비활성 타임아웃: 장기간 방치된 터미널 세션을 자동으로 해제한다.
            클라이언트로부터 JSON 형태로 stdin 데이터 또는 화면 크기(resize) 이벤트를 수신한다.
            """
            import json

            try:
                while True:
                    data = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=3600.0,  # 1시간 비활성 타임아웃
                    )
                    if not resp.is_open():
                        break

                    parsed_successfully = False
                    try:
                        parsed = json.loads(data)
                        if isinstance(parsed, dict) and "type" in parsed:
                            msg_type = parsed.get("type")
                            if msg_type == "stdin":
                                resp.write_stdin(parsed.get("data", ""))
                            elif msg_type == "resize":
                                cols = parsed.get("cols", 80)
                                rows = parsed.get("rows", 24)
                                # Channel 4 is used for terminal resize commands in kubernetes.stream
                                resize_msg = json.dumps({"Width": cols, "Height": rows})
                                resp.write_channel(4, resize_msg)
                            parsed_successfully = True
                    except Exception:
                        pass

                    # Fallback: 구버전 클라이언트 호환성 유지 및 예외 처리
                    if not parsed_successfully:
                        resp.write_stdin(data)

            except asyncio.TimeoutError:
                print(
                    f"WebSocket {playground_id} closed due to inactivity timeout (1 hour)."
                )
            except WebSocketDisconnect:
                pass  # 브라우저가 탭을 닫거나 정상 종료한 경우
            except Exception as e:
                print(f"WS -> K8s Error: {e}")
            finally:
                resp.close()  # k8s exec 스트림 종료

        # 두 방향 스트리밍을 동시에 실행하며, 어느 한쪽이 종료되면 함께 종료된다.
        await asyncio.gather(k8s_to_ws(), ws_to_k8s())

    except Exception as e:
        print(f"WebSocket Error: {e}")
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    # 개발 환경에서 직접 실행 시 사용. 프로덕션 K8s 배포에서는 Dockerfile CMD를 통해 기동된다.
    uvicorn.run(app, host="0.0.0.0", port=8000)
