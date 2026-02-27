"""
session_manager.py — 익명 UUID 기반 세션 관리 (Kubernetes Native)

역할:
- 브라우저마다 UUID v4 세션 쿠키(session_id)를 발급한다.
- 세션과 플레이그라운드의 매핑을 Kubernetes ConfigMap으로 관리한다.
  (과거 SQLite sessions.db 방식에서 K8s Native 방식으로 변경됨)
- 1 세션 = 1 플레이그라운드 정책을 강제한다.
- 만료된 세션 ConfigMap을 주기적으로 정리한다.

[설계 이유] ConfigMap 저장 방식의 장점:
- 외부 DB 없이 K8s 클러스터 자체가 세션 저장소가 된다.
- API 서버 재시작 후에도 세션이 유지된다(NFS PVC 불필요).
- kubectl로 세션 현황을 직접 확인/조작할 수 있다.

[보안 참고] ConfigMap은 Secret과 달리 기본적으로 etcd 암호화 대상이 아니다.
세션 정보(playground_id, cluster_id)는 민감하지 않으나, 운영 환경에서는
etcd 암호화 또는 Secret 사용을 고려할 수 있다.
"""

import os
import uuid
import datetime
from kubernetes import client
from kubernetes.client.rest import ApiException
from dotenv import load_dotenv

from utils import get_k8s_client

# .env 파일이 있으면 환경변수로 로드 (개발 환경 편의용)
load_dotenv()

# 세션 ConfigMap이 저장될 네임스페이스. 플랫폼 네임스페이스와 동일하게 사용한다.
PLATFORM_NAMESPACE = os.getenv("NAMESPACE_PLATFORM", "playground-system")


# 모듈 수준 싱글턴 CoreV1Api 클라이언트.
# 매 요청마다 새 클라이언트를 만들지 않아 연결 오버헤드를 줄인다.
_core_v1 = None


def _api() -> client.CoreV1Api:
    """
    싱글턴 CoreV1Api 인스턴스를 반환한다.
    첫 호출 시에만 utils.get_k8s_client()를 통해 초기화된다.
    """
    global _core_v1
    if not _core_v1:
        _core_v1, _, _ = get_k8s_client()
    return _core_v1


def _cm_name(session_id: str) -> str:
    """세션 ID로 ConfigMap 이름을 생성한다. K8s 리소스 이름 규칙을 따른다."""
    return f"playground-session-{session_id}"


# ── 세션 생성/조회 ──────────────────────────────────────────────


def get_or_create_session_id(request, response) -> str:
    """
    요청의 세션 쿠키를 확인하고, 없으면 새로 발급한다.

    쿠키 보안 설정:
    - httponly=True: JavaScript에서 document.cookie로 접근 불가 (XSS 방지)
    - samesite="strict": 외부 사이트에서의 요청에 쿠키가 포함되지 않음 (CSRF 방지)
    - max_age=86400: 24시간 후 자동 만료

    ConfigMap 생성은 best-effort: 409(이미 존재) 이외의 오류는 로그만 출력하고
    세션 ID는 반환한다 (쿠키만 있어도 일부 기능은 동작할 수 있음).
    """
    session_id = request.cookies.get("session_id")
    if not session_id:
        # 새 UUID v4 세션 ID 발급 (추적 불가능한 무작위 값)
        session_id = str(uuid.uuid4())
        expires = datetime.datetime.utcnow() + datetime.timedelta(hours=24)

        # 보안 속성이 설정된 HTTP 쿠키로 클라이언트에 전달
        response.set_cookie(
            key="session_id",
            value=session_id,
            httponly=True,  # JavaScript 접근 차단
            samesite="strict",  # CSRF 방지
            max_age=86400,  # 24시간
        )

        # K8s ConfigMap에 세션 메타데이터 저장
        cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=_cm_name(session_id),
                namespace=PLATFORM_NAMESPACE,
                labels={
                    "playground.k8s-playground.io/type": "session"
                },  # 레이블로 일괄 조회 가능
            ),
            data={
                "created_at": datetime.datetime.utcnow().isoformat(),
                "expires_at": expires.isoformat(),
                "playground_id": "",  # 아직 바인딩 전이므로 빈 값
                "cluster_id": "",
            },
        )
        try:
            _api().create_namespaced_config_map(namespace=PLATFORM_NAMESPACE, body=cm)
        except client.exceptions.ApiException as e:
            if e.status != 409:
                print(f"Failed to create session CM: {e}")
    return session_id


def get_active_playground(session_id: str) -> dict | None:
    """
    세션에 바인딩된 활성 플레이그라운드 정보를 반환한다.

    만료 처리:
    - ConfigMap에 저장된 expires_at을 현재 시간과 비교한다.
    - 만료된 경우 playground_id/cluster_id 필드를 비운다(바인딩 해제).
    - ConfigMap 자체는 cleanup_expired_sessions()에서 별도로 삭제된다.

    Returns:
        {"playground_id": str, "cluster_id": str} 또는 None
    """
    try:
        cm = _api().read_namespaced_config_map(
            name=_cm_name(session_id), namespace=PLATFORM_NAMESPACE
        )
        data = cm.data or {}
        playground_id = data.get("playground_id")
        cluster_id = data.get("cluster_id")
        expires_at = data.get("expires_at")

        if playground_id and expires_at:
            exp = datetime.datetime.fromisoformat(expires_at)
            if datetime.datetime.utcnow() < exp:
                return {"playground_id": playground_id, "cluster_id": cluster_id}
            else:
                # 만료 → 바인딩만 해제하고 ConfigMap 자체는 유지
                clear_playground(session_id)
        return None
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return None  # 세션 ConfigMap이 없으면 조용히 None 반환
        print(f"Error reading active playground: {e}")
        return None


def bind_playground(session_id: str, playground_id: str, cluster_id: str):
    """
    세션 ConfigMap에 플레이그라운드 정보를 기록한다(바인딩).
    만료 시간도 24시간 후로 갱신한다.

    ConfigMap이 없는 경우(세션 쿠키는 있지만 ConfigMap이 삭제된 경우):
    새 ConfigMap을 생성하여 세션을 복구한다.
    """
    expires = datetime.datetime.utcnow() + datetime.timedelta(hours=24)

    # ConfigMap 초기 생성 / 읽기
    try:
        cm = _api().read_namespaced_config_map(
            name=_cm_name(session_id), namespace=PLATFORM_NAMESPACE
        )
    except ApiException as e:
        if e.status == 404:
            # 404: ConfigMap이 아직 없으면 새로 생성한다.
            cm = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name=_cm_name(session_id),
                    namespace=PLATFORM_NAMESPACE,
                    labels={"playground.k8s-playground.io/type": "session"},
                ),
                data={
                    "created_at": datetime.datetime.utcnow().isoformat(),
                    "expires_at": expires.isoformat(),
                },
            )
            try:
                _api().create_namespaced_config_map(
                    namespace=PLATFORM_NAMESPACE, body=cm
                )
                # 생성 후 다시 읽어서 최신 상태를 가져온다.
                cm = _api().read_namespaced_config_map(
                    name=_cm_name(session_id), namespace=PLATFORM_NAMESPACE
                )
            except ApiException as ce:
                # 동시 요청으로 이미 생성되었을 경우
                if ce.status == 409:
                    # 이미 생성되었으므로 다시 읽는다.
                    cm = _api().read_namespaced_config_map(
                        name=_cm_name(session_id), namespace=PLATFORM_NAMESPACE
                    )
                else:
                    print(f"Failed to create session ConfigMap: {ce}")
                    return  # 오류 발생 시 함수 종료
        else:
            print(f"Failed to read session ConfigMap: {e}")
            return  # 오류 발생 시 함수 종료

    # ConfigMap이 존재하거나 새로 생성된 경우, 데이터 업데이트
    patch = {
        "data": {
            "playground_id": playground_id,
            "cluster_id": cluster_id,
            "expires_at": expires.isoformat(),
        }
    }
    try:
        _api().patch_namespaced_config_map(
            name=_cm_name(session_id), namespace=PLATFORM_NAMESPACE, body=patch
        )
    except ApiException as e:
        print(f"Error binding playground (API failure): {e}")


def clear_playground(session_id: str):
    """
    세션 ConfigMap에서 플레이그라운드 바인딩을 해제한다.
    ConfigMap 자체는 삭제하지 않고 playground_id/cluster_id 필드만 비운다.
    오류가 발생해도 조용히 무시한다 (이미 삭제된 ConfigMap 등).
    """
    patch = {"data": {"playground_id": "", "cluster_id": ""}}
    try:
        _api().patch_namespaced_config_map(
            name=_cm_name(session_id), namespace=PLATFORM_NAMESPACE, body=patch
        )
    except ApiException as e:
        if e.status != 404:
            print(f"Error clearing playground (API failure): {e}")


def clear_all_sessions_for_playground(playground_id: str):
    """
    특정 플레이그라운드에 바인딩된 모든 세션의 바인딩을 해제한다.
    플레이그라운드 삭제 시 호출되어 어떤 세션도 삭제된 플레이그라운드를
    가리키지 않도록 보장한다.

    레이블 셀렉터로 세션 ConfigMap만 조회하여 효율적으로 처리한다.
    """
    try:
        cms = _api().list_namespaced_config_map(
            namespace=PLATFORM_NAMESPACE,
            label_selector="playground.k8s-playground.io/type=session",
        )
        for cm in cms.items:
            if cm.data and cm.data.get("playground_id") == playground_id:
                # ConfigMap 이름에서 세션 ID 부분만 추출
                session_id = cm.metadata.name.replace("playground-session-", "")
                clear_playground(session_id)
    except ApiException as e:
        print(f"Failed to clear sessions for playground: {e}")


def cleanup_expired_sessions():
    """
    만료된 세션 ConfigMap을 삭제하여 playground-system 네임스페이스를 정리한다.
    백그라운드 정리 루프(main.py lifespan)에서 60초마다 호출된다.

    처리 기준: expires_at이 현재 UTC 시간보다 이전인 ConfigMap을 삭제한다.
    playground_id가 있든 없든 만료된 ConfigMap은 모두 삭제한다.

    [운영 효과]
    - etcd에 세션 ConfigMap이 무한 축적되는 것을 방지한다.
    - 세션 조회 시 list 응답 크기를 줄여 성능을 유지한다.
    """
    try:
        cms = _api().list_namespaced_config_map(
            namespace=PLATFORM_NAMESPACE,
            label_selector="playground.k8s-playground.io/type=session",
        )
        now = datetime.datetime.utcnow()
        deleted = 0
        for cm in cms.items:
            expires_at_str = (cm.data or {}).get("expires_at", "")
            if not expires_at_str:
                continue
            try:
                exp = datetime.datetime.fromisoformat(expires_at_str)
                if now > exp:
                    _api().delete_namespaced_config_map(
                        name=cm.metadata.name, namespace=PLATFORM_NAMESPACE
                    )
                    deleted += 1
            except Exception as e:
                print(f"Error processing session CM {cm.metadata.name}: {e}")
        if deleted > 0:
            print(f"Cleaned up {deleted} expired session ConfigMaps.")
    except client.exceptions.ApiException as e:
        print(f"Failed to cleanup expired sessions: {e}")
