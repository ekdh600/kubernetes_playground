"""
cluster_registry.py — 멀티 클러스터 kubeconfig 레지스트리 (Kubernetes Native)

역할:
- 외부 Kubernetes 클러스터의 kubeconfig를 K8s Secret으로 등록/조회/삭제한다.
- 클러스터별 K8sManager 인스턴스를 생성하고 인메모리 캐시로 재사용한다.
- 상태를 외부 DB 없이 K8s API(Secret)에만 의존하는 Stateless 설계다.

[저장 방식]
- Secret 이름: playground-cluster-{cluster_id}
- 네임스페이스: NAMESPACE_PLATFORM (기본값: playground-system)
- Secret.data 필드에 base64로 인코딩하여 저장
  (K8s API는 data 필드 값을 base64로 관리하므로 수동 인코딩 필요)

[캐시 설계]
- K8sManager는 생성 비용(kubeconfig 파싱, 연결 풀 초기화)이 있으므로
  인메모리 딕셔너리에 클러스터 ID를 키로 캐싱한다.
- 클러스터 삭제 또는 kubeconfig 변경 시 invalidate_cache()로 갱신해야 한다.
"""

import os
import uuid
import base64
import datetime
import yaml
from kubernetes import client
from k8s_manager import K8sManager
from dotenv import load_dotenv
from utils import get_k8s_client

# .env 파일 환경변수 로드 (개발 환경 편의용)
load_dotenv()

# 클러스터 Secret이 저장될 네임스페이스
PLATFORM_NAMESPACE = os.getenv("NAMESPACE_PLATFORM", "playground-system")


# 모듈 수준 싱글턴 클라이언트 (매 호출마다 초기화 오버헤드 방지)
_core_v1 = None


def _api() -> client.CoreV1Api:
    """싱글턴 CoreV1Api를 반환한다. 첫 호출 시에만 초기화된다."""
    global _core_v1
    if not _core_v1:
        _core_v1, _, _ = get_k8s_client()
    return _core_v1


# ── Secret 데이터 인코딩 유틸리티 ────────────────────────────
# K8s Secret.data 필드는 base64 인코딩된 값을 요구한다.
# string_data를 사용하면 자동 인코딩되지만, 조회 시에는 항상 base64 형태로 반환되므로
# 일관성을 위해 저장 시에도 수동으로 base64 인코딩한다.


def _b64enc(s: str) -> str:
    """문자열을 base64로 인코딩하여 K8s Secret.data에 저장 가능한 형태로 변환한다."""
    return base64.b64encode(s.encode("utf-8")).decode("utf-8")


def _b64dec(s: str) -> str:
    """K8s Secret.data에서 읽어온 base64 문자열을 디코딩한다."""
    return base64.b64decode(s.encode("utf-8")).decode("utf-8")


def _secret_name(cluster_id: str) -> str:
    """클러스터 ID를 K8s Secret 이름으로 변환한다."""
    return f"playground-cluster-{cluster_id}"


# ── 클러스터 CRUD ─────────────────────────────────────────────


def register_cluster(name: str, kubeconfig_yaml: str) -> str:
    """
    새 클러스터를 레지스트리에 등록한다.
    kubeconfig YAML 전체를 base64로 인코딩하여 K8s Secret에 저장한다.

    Returns:
        생성된 클러스터 ID (UUID v4 앞 8자리)
    """
    # E-3: YAML 유효성 검증
    try:
        parsed_yaml = yaml.safe_load(kubeconfig_yaml)
        if not isinstance(parsed_yaml, dict):
            raise ValueError("Invalid YAML format")
        required_keys = ["clusters", "users", "contexts"]
        if not all(k in parsed_yaml for k in required_keys):
            raise ValueError(f"Missing required fields: {required_keys}")
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse kubeconfig YAML: {e}")

    # 추가 검증: 인증 정보로 K8s 클라이언트를 로드할 수 있는지 테스트
    try:
        test_client, _, _ = get_k8s_client(kubeconfig_path=parsed_yaml)
        test_client.list_namespace(limit=1)
    except Exception as e:
        raise ValueError(f"Failed to connect to cluster with provided kubeconfig: {e}")

    cluster_id = str(uuid.uuid4())[:8]
    meta = client.V1ObjectMeta(
        name=_secret_name(cluster_id),
        namespace=PLATFORM_NAMESPACE,
        # 레이블: list_clusters()에서 type=cluster인 Secret만 필터링하는 데 사용
        labels={"playground.k8s-playground.io/type": "cluster"},
    )
    secret = client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=meta,
        type="Opaque",
        data={
            "id": _b64enc(cluster_id),
            "name": _b64enc(name),
            "kubeconfig": _b64enc(kubeconfig_yaml),  # 민감 데이터: kubeconfig 전체 내용
            "created_at": _b64enc(datetime.datetime.utcnow().isoformat()),
        },
    )
    _api().create_namespaced_secret(namespace=PLATFORM_NAMESPACE, body=secret)
    return cluster_id


def list_clusters() -> list[dict]:
    """
    등록된 모든 클러스터의 메타데이터 목록을 반환한다.
    kubeconfig는 포함하지 않아 민감 정보 노출을 방지한다.

    레이블 셀렉터(type=cluster)로 플랫폼의 다른 Secret과 구분한다.
    네임스페이스가 없는 경우(첫 배포 전) 빈 리스트를 반환한다.

    Returns:
        [{"id": str, "name": str, "created_at": str}, ...] 생성 시간 오름차순 정렬
    """
    try:
        secrets = _api().list_namespaced_secret(
            namespace=PLATFORM_NAMESPACE,
            label_selector="playground.k8s-playground.io/type=cluster",
        )
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return []  # 네임스페이스 자체가 없는 경우 (초기 배포 전)
        raise

    clusters = []
    for s in secrets.items:
        if not s.data:
            continue
        clusters.append(
            {
                "id": _b64dec(s.data.get("id", "")),
                "name": _b64dec(s.data.get("name", "")),
                "created_at": _b64dec(s.data.get("created_at", "")),
                # kubeconfig는 절대 반환하지 않는다 (보안)
            }
        )
    return sorted(clusters, key=lambda x: x["created_at"])


def get_cluster(cluster_id: str) -> dict | None:
    """
    클러스터 ID로 메타데이터를 조회한다 (kubeconfig 제외).
    존재하지 않는 경우 None을 반환한다.
    """
    try:
        s = _api().read_namespaced_secret(
            name=_secret_name(cluster_id), namespace=PLATFORM_NAMESPACE
        )
        if not s.data:
            return None
        return {
            "id": _b64dec(s.data.get("id", "")),
            "name": _b64dec(s.data.get("name", "")),
            "created_at": _b64dec(s.data.get("created_at", "")),
        }
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return None
        raise


def delete_cluster(cluster_id: str):
    """
    클러스터 등록을 해제한다 (K8s Secret 삭제).
    이미 삭제된 경우(404)는 성공으로 처리한다.
    인메모리 캐시도 함께 무효화한다.
    """
    try:
        _api().delete_namespaced_secret(
            name=_secret_name(cluster_id), namespace=PLATFORM_NAMESPACE
        )
        invalidate_cache(cluster_id)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise


# ── K8sManager 팩토리 ─────────────────────────────────────────


def get_manager(cluster_id: str) -> K8sManager:
    """
    클러스터 ID에 해당하는 K8sManager 인스턴스를 생성한다.
    K8s Secret에서 kubeconfig를 읽어 yaml.safe_load()로 파싱한 뒤
    K8sManager에 dict 형태로 전달한다 (파일 경유 없이 인메모리 처리).

    server_url 추출:
    kubeconfig의 clusters[0].cluster.server 값을 추출하여 K8sManager에 전달한다.
    이 값이 create_kubeconfig_secret()에서 파드 내 kubectl의 API 서버 주소로 사용된다.

    Raises:
        ValueError: 클러스터 ID가 레지스트리에 없는 경우
    """
    try:
        s = _api().read_namespaced_secret(
            name=_secret_name(cluster_id), namespace=PLATFORM_NAMESPACE
        )
    except client.exceptions.ApiException as e:
        if e.status == 404:
            raise ValueError(f"Cluster {cluster_id} not found")
        raise

    kubeconfig_yaml = _b64dec(s.data.get("kubeconfig", ""))

    # YAML 문자열 → dict: K8sManager는 파일이 아닌 dict로만 kubeconfig를 받는다.
    kubeconfig_dict = yaml.safe_load(kubeconfig_yaml)

    # kubeconfig에서 API 서버 URL을 추출한다.
    # 이 URL은 파드 내 kubectl이 접속할 실제 K8s API 주소가 된다.
    server_url = None
    try:
        server_url = (
            kubeconfig_dict.get("clusters", [])[0].get("cluster", {}).get("server")
        )
    except Exception:
        pass  # 파싱 실패 시 K8sManager 기본값(https://kubernetes.default.svc) 사용

    ca_data = None
    try:
        ca_data = (
            kubeconfig_dict.get("clusters", [])[0]
            .get("cluster", {})
            .get("certificate-authority-data")
        )
    except Exception:
        pass

    return K8sManager(
        cluster_id=cluster_id,
        kubeconfig_dict=kubeconfig_dict,
        server_url=server_url,
        ca_data=ca_data,
    )


# ── 인메모리 캐시 ──────────────────────────────────────────────
# K8sManager는 생성 시 kubeconfig 파싱, SSL 설정, 연결 풀 초기화 등 비용이 발생한다.
# 클러스터 ID를 키로 딕셔너리에 캐싱하여 매 API 요청마다 재생성하지 않는다.
# [주의] 멀티 워커(uvicorn worker > 1) 환경에서는 프로세스별로 캐시가 독립적이다.
_manager_cache: dict[str, K8sManager] = {}


def get_cached_manager(cluster_id: str) -> K8sManager:
    """
    캐시된 K8sManager를 반환한다.
    캐시에 없으면 get_manager()로 새로 생성하여 캐시에 등록한 뒤 반환한다.
    """
    if cluster_id not in _manager_cache:
        _manager_cache[cluster_id] = get_manager(cluster_id)
    return _manager_cache[cluster_id]


def invalidate_cache(cluster_id: str):
    """
    특정 클러스터의 캐시된 K8sManager를 제거한다.
    클러스터 삭제 또는 kubeconfig가 변경된 경우 반드시 호출해야
    이후 요청에서 갱신된 설정으로 새 매니저가 생성된다.
    """
    _manager_cache.pop(cluster_id, None)
