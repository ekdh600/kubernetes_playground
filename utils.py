from kubernetes import client, config
import os


def get_k8s_client(kubeconfig_path=None):
    """
    주어진 kubeconfig(dict 또는 파일 경로)로 K8s 클라이언트를 초기화하여 반환한다.
    만약 kubeconfig가 제공되지 않으면 기본 동작(InCluster -> os.getenv("KUBECONFIG") 기본값)을 수행한다.
    """
    if kubeconfig_path is None:
        kubeconfig_path = os.getenv("KUBECONFIG", "kube_config.yaml")

    if isinstance(kubeconfig_path, dict):
        # 딕셔너리 형태의 kubeconfig 데이터 (멀티 클러스터 동적 로드)
        api_client = config.new_client_from_config_dict(kubeconfig_path)
        core_v1 = client.CoreV1Api(api_client)
        rbac_v1 = client.RbacAuthorizationV1Api(api_client)
        apps_v1 = client.AppsV1Api(api_client)
        return core_v1, rbac_v1, apps_v1
    else:
        # 파일 기반 (또는 InCluster)
        try:
            config.load_incluster_config()
        except config.ConfigException:
            try:
                config.load_kube_config(config_file=kubeconfig_path)
            except Exception as e:
                raise RuntimeError(
                    f"K8s Config load failed: InCluster and {kubeconfig_path} failed: {e}"
                )
        core_v1 = client.CoreV1Api()
        rbac_v1 = client.RbacAuthorizationV1Api()
        apps_v1 = client.AppsV1Api()
        return core_v1, rbac_v1, apps_v1
