"""
k8s_manager.py — Kubernetes 리소스 생명주기 관리자

역할:
- FastAPI 백엔드와 Kubernetes API 서버 사이의 인터페이스 역할을 한다.
- 클러스터마다 독립적인 K8sManager 인스턴스를 생성하여 멀티 클러스터를 지원한다.
- 플레이그라운드별로 네임스페이스, RBAC, Secret, Deployment, Service를 생성/삭제한다.

[설계 원칙]
- 각 K8sManager는 독립적인 ApiClient를 가지므로 전역 K8s 설정이 오염되지 않는다.
- kubeconfig는 인메모리 dict로 로드하여 임시 파일을 만들지 않는다(보안).
- 409(이미 존재) 오류는 멱등성을 위해 성공으로 처리한다.
"""

import base64
import os
import time
import datetime
from kubernetes import client, config


class K8sManager:
    def __init__(
        self,
        cluster_id: str,
        kubeconfig_dict: dict,
        server_url: str = None,
        ca_data: str = None,
    ):
        """
        클러스터별 독립 K8sManager를 초기화한다.

        Args:
            cluster_id: 클러스터 식별자. 레이블 필터링에 사용된다.
            kubeconfig_dict: yaml.safe_load()로 파싱된 kubeconfig 딕셔너리.
                             파일 경로 대신 인메모리 dict를 사용하여 임시 파일 노출을 방지한다.
            server_url: kubeconfig에서 추출한 실제 API 서버 URL.
                        None이면 "https://kubernetes.default.svc"를 사용한다.
                        멀티클러스터에서는 외부 클러스터의 실제 URL을 전달해야
                        파드 내 kubectl이 올바른 API 서버에 접속한다.
        """
        self.cluster_id = cluster_id
        # server_url은 create_kubeconfig_secret()에서 사용된다.
        # 멀티클러스터 환경에서 kubeconfig의 실제 server URL을 전달받아야 정확한 동작이 보장된다.
        self.server_url = server_url or "https://kubernetes.default.svc"
        self.ca_data = ca_data

        # 인스턴스별 독립 Configuration 객체 생성 (전역 상태 오염 방지)
        api_config = client.Configuration()

        if kubeconfig_dict is not None:
            # 파일 없이 dict에서 직접 로드. 민감한 kubeconfig가 디스크에 쓰이지 않는다.
            config.load_kube_config_from_dict(
                config_dict=kubeconfig_dict, client_configuration=api_config
            )
            print(f"Loaded kubeconfig from in-memory dict for cluster {cluster_id}")
        else:
            raise ValueError(
                "kubeconfig_dict must be provided. "
                "File-based kubeconfig is no longer supported for registered clusters."
            )

        # KUBE_INSECURE=true 환경변수가 있을 때만 SSL 검증을 비활성화한다.
        # 개발/자체서명 인증서 환경에서만 사용하며, 프로덕션에서는 반드시 false여야 한다.
        kube_insecure = os.getenv("KUBE_INSECURE", "false").lower() == "true"
        if kube_insecure:
            import urllib3

            api_config.verify_ssl = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            print("[WARNING] SSL verification disabled (KUBE_INSECURE=true).")
        else:
            api_config.verify_ssl = True

        # 독립 ApiClient: 클러스터별로 분리된 연결 풀을 가진다.
        api_client = client.ApiClient(configuration=api_config)

        # 응답 없는 클러스터(잘못된 설정 등)에 영원히 대기하지 않도록 10초 타임아웃 설정
        api_client.rest_client.pool_manager.connection_pool_kw["timeout"] = 10.0

        # 용도별 API 클라이언트
        self.core_v1 = client.CoreV1Api(
            api_client
        )  # Pod, Service, Secret, Namespace 등
        self.apps_v1 = client.AppsV1Api(api_client)  # Deployment
        self.rbac_v1 = client.RbacAuthorizationV1Api(api_client)  # Role, ClusterRole 등

    # ── 네임스페이스 ─────────────────────────────────────────────

    def setup_namespace(self, namespace: str):
        """
        플레이그라운드 호스트 네임스페이스(기본값: study)를 초기화한다.
        네임스페이스와 플레이그라운드 파드 실행에 필요한 ServiceAccount를 생성한다.
        이미 존재하는 경우(409)는 성공으로 처리한다.

        [참고] playground-sa는 파드의 K8s 서비스 계정으로만 사용되며,
        실제 kubectl 접근 권한은 kubeconfig Secret에 담긴 별도 SA 토큰이 담당한다.
        """
        # 네임스페이스 생성
        try:
            ns = client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace))
            self.core_v1.create_namespace(body=ns)
            print(f"Namespace {namespace} created.")
        except client.exceptions.ApiException as e:
            if e.status != 409:  # 이미 존재하는 경우가 아닌 진짜 오류만 출력
                print(f"Error creating namespace: {e}")

        # 파드 실행에 필요한 최소 ServiceAccount 생성
        sa_name = "playground-sa"
        try:
            sa = client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name=sa_name, namespace=namespace)
            )
            self.core_v1.create_namespaced_service_account(namespace=namespace, body=sa)
            print(f"ServiceAccount {sa_name} created.")
        except client.exceptions.ApiException as e:
            if e.status != 409:
                print(f"Error creating SA: {e}")

    def list_namespaces(self) -> list[str]:
        """
        클러스터의 전체 네임스페이스 이름 목록을 반환한다.
        관리자 UI에서 커스텀 플레이그라운드 생성 시 네임스페이스 선택 옵션을 제공한다.
        """
        try:
            ns_list = self.core_v1.list_namespace()
            return [ns.metadata.name for ns in ns_list.items]
        except Exception as e:
            print(f"Error listing namespaces: {e}")
            return []

    def create_sandbox_namespace(self, sandbox_ns: str, playground_id: str):
        """
        사용자 전용 격리 네임스페이스를 생성한다.
        이 네임스페이스 안에서만 kubectl 명령을 실행할 수 있도록 RBAC이 구성된다.
        플레이그라운드 삭제 시 이 네임스페이스 전체를 지워 모든 자원을 정리한다.
        """
        metadata = client.V1ObjectMeta(
            name=sandbox_ns,
            labels={
                "app": "playground-sandbox",
                "instance": playground_id,
                "cluster": self.cluster_id,
            },
        )
        body = client.V1Namespace(metadata=metadata)
        try:
            self.core_v1.create_namespace(body=body)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                return
            raise e

    # ── RBAC ─────────────────────────────────────────────────────

    def setup_sandbox_rbac(self, sandbox_ns: str) -> str:
        """
        일반 사용자 플레이그라운드를 위한 RBAC을 설정한다.
        sandbox 네임스페이스 전용 ServiceAccount에 ClusterRole "admin"을 바인딩한다.

        [설계 의도] ClusterRole "admin"은 sandbox 네임스페이스 내에서만 유효한
        RoleBinding으로 바인딩되므로, 사용자는 자신의 sandbox 내에서만 관리자 권한을 갖는다.
        다른 네임스페이스에는 접근할 수 없어 격리가 보장된다.

        Returns:
            생성된 ServiceAccount 이름 ("admin-sa")
        """
        sa_name = "admin-sa"

        # sandbox 네임스페이스 전용 ServiceAccount 생성
        try:
            sa = client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name=sa_name, namespace=sandbox_ns)
            )
            self.core_v1.create_namespaced_service_account(
                namespace=sandbox_ns, body=sa
            )
        except client.exceptions.ApiException as e:
            if e.status != 409:
                print(f"Error creating Sandbox SA: {e}")

        # ClusterRole "admin"을 sandbox 네임스페이스에 바인딩
        # RoleBinding이므로 클러스터 전체가 아닌 sandbox 내에서만 권한이 적용된다.
        try:
            rb = client.V1RoleBinding(
                metadata=client.V1ObjectMeta(name="admin-rb", namespace=sandbox_ns),
                subjects=[
                    client.RbacV1Subject(
                        kind="ServiceAccount", name=sa_name, namespace=sandbox_ns
                    )
                ],
                role_ref=client.V1RoleRef(
                    kind="ClusterRole",
                    name="admin",  # K8s 내장 ClusterRole: pod, svc, deploy 등 전체 관리 권한
                    api_group="rbac.authorization.k8s.io",
                ),
            )
            self.rbac_v1.create_namespaced_role_binding(namespace=sandbox_ns, body=rb)
        except client.exceptions.ApiException as e:
            if e.status != 409:
                print(f"Error creating Sandbox RB: {e}")

        return sa_name

    def setup_custom_rbac(
        self,
        sandbox_ns: str,
        namespaces: list[str],
        verbs: list[str],
        playground_id: str,
    ) -> str:
        """
        관리자가 지정한 네임스페이스 범위와 동사로 커스텀 RBAC을 설정한다.
        모든 생성된 Role/ClusterRole에 instance/cluster 레이블을 붙여
        삭제 시 레이블 셀렉터로 일괄 제거할 수 있게 한다.

        Args:
            sandbox_ns: SA가 생성될 네임스페이스 (sandbox-{id})
            namespaces: 권한을 부여할 네임스페이스 목록.
                        ["*"] = 클러스터 전체 (ClusterRole+ClusterRoleBinding),
                        ["sandbox"] = 전용 sandbox 네임스페이스로 자동 치환,
                        그 외 = 각 네임스페이스에 Role+RoleBinding 생성
            verbs: 허용할 kubectl 동사 목록 (예: ["get","list","watch"])
                   [보안 주의] "create","delete","patch" 등 write 동사와 namespaces=["*"] 조합은
                   클러스터 전체 쓰기 권한을 부여하므로 교육 목적에서만 사용해야 한다.

        Returns:
            생성된 ServiceAccount 이름 ("admin-sa")
        """
        sa_name = "admin-sa"

        # sandbox 네임스페이스 전용 SA 생성
        try:
            sa = client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name=sa_name, namespace=sandbox_ns)
            )
            self.core_v1.create_namespaced_service_account(
                namespace=sandbox_ns, body=sa
            )
        except client.exceptions.ApiException as e:
            if e.status != 409:
                print(f"Error creating Sandbox SA: {e}")

        # 레이블: 삭제 시 label_selector로 이 플레이그라운드의 커스텀 RBAC만 필터링한다.
        labels = {"instance": playground_id, "cluster": self.cluster_id}
        role_name = f"playground-custom-{playground_id}"

        # api_groups=["*", ""]로 설정하는 이유:
        # - "": core API group (pods, services 등)
        # - "*": 모든 확장 API group (apps, rbac.authorization.k8s.io 등)
        policy_rule = client.V1PolicyRule(
            api_groups=["*", ""], resources=["*"], verbs=verbs
        )

        if "*" in namespaces:
            # 클러스터 전체 범위 권한: ClusterRole + ClusterRoleBinding 생성
            try:
                cr = client.V1ClusterRole(
                    metadata=client.V1ObjectMeta(name=role_name, labels=labels),
                    rules=[policy_rule],
                )
                self.rbac_v1.create_cluster_role(body=cr)
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    print(f"Error creating custom ClusterRole: {e}")

            try:
                crb = client.V1ClusterRoleBinding(
                    metadata=client.V1ObjectMeta(name=role_name, labels=labels),
                    subjects=[
                        client.RbacV1Subject(
                            kind="ServiceAccount", name=sa_name, namespace=sandbox_ns
                        )
                    ],
                    role_ref=client.V1RoleRef(
                        kind="ClusterRole",
                        name=role_name,
                        api_group="rbac.authorization.k8s.io",
                    ),
                )
                self.rbac_v1.create_cluster_role_binding(body=crb)
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    print(f"Error creating custom ClusterRoleBinding: {e}")
        else:
            # 지정된 네임스페이스마다 Role + RoleBinding 생성
            # "sandbox" 키워드는 실제 sandbox 네임스페이스 이름으로 치환한다.
            resolved_namespaces = []
            for ns in namespaces:
                ns = ns.strip()
                if not ns:
                    continue
                if ns.lower() == "sandbox":
                    resolved_namespaces.append(sandbox_ns)
                else:
                    resolved_namespaces.append(ns)

            # 중복 제거 (같은 네임스페이스에 두 번 바인딩하지 않도록)
            resolved_namespaces = list(set(resolved_namespaces))

            for ns in resolved_namespaces:
                try:
                    role = client.V1Role(
                        metadata=client.V1ObjectMeta(
                            name=role_name, namespace=ns, labels=labels
                        ),
                        rules=[policy_rule],
                    )
                    self.rbac_v1.create_namespaced_role(namespace=ns, body=role)
                except client.exceptions.ApiException as e:
                    if e.status != 409:
                        print(f"Error creating custom Role in {ns}: {e}")

                try:
                    rb = client.V1RoleBinding(
                        metadata=client.V1ObjectMeta(
                            name=role_name, namespace=ns, labels=labels
                        ),
                        subjects=[
                            client.RbacV1Subject(
                                kind="ServiceAccount",
                                name=sa_name,
                                namespace=sandbox_ns,
                            )
                        ],
                        role_ref=client.V1RoleRef(
                            kind="Role",
                            name=role_name,
                            api_group="rbac.authorization.k8s.io",
                        ),
                    )
                    self.rbac_v1.create_namespaced_role_binding(namespace=ns, body=rb)
                except client.exceptions.ApiException as e:
                    if e.status != 409:
                        print(f"Error creating custom RoleBinding in {ns}: {e}")

        return sa_name

    # ── ServiceAccount 토큰 ─────────────────────────────────────

    def get_service_account_token(self, namespace: str, sa_name: str) -> str | None:
        """
        ServiceAccount의 JWT 토큰을 가져온다 (Legacy SA Token Secret 방식).

        Kubernetes 1.24+ 부터는 TokenRequest API가 권장되지만,
        여기서는 SA에 연결된 Secret 자동 생성 방식을 사용한다.
        이유: TokenRequest API 토큰은 만료 시간이 있어 장기 실행 플레이그라운드에서
        kubectl 접근이 중단될 수 있다. Legacy 방식은 Secret이 삭제될 때까지 유효하다.

        동작:
        1. kubernetes.io/service-account-token 타입 Secret을 생성한다.
        2. K8s 컨트롤러가 Secret에 토큰을 채울 때까지 최대 10초 폴링한다.

        Returns:
            JWT 토큰 문자열 또는 None (타임아웃 시)
        """
        secret_name = f"{sa_name}-token"

        # SA와 연결된 토큰 Secret 생성 요청
        try:
            secret = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=secret_name,
                    namespace=namespace,
                    annotations={"kubernetes.io/service-account.name": sa_name},
                ),
                type="kubernetes.io/service-account-token",
            )
            self.core_v1.create_namespaced_secret(namespace=namespace, body=secret)
            print(f"Created token secret {secret_name}")
        except client.exceptions.ApiException as e:
            if e.status != 409:
                print(f"Error creating token secret: {e}")
                return None

        # K8s 컨트롤러가 토큰을 Secret에 기록할 때까지 최대 10초 대기
        for _ in range(10):
            try:
                s = self.core_v1.read_namespaced_secret(
                    name=secret_name, namespace=namespace
                )
                if s.data and "token" in s.data:
                    # K8s API는 Secret.data 값을 base64로 반환하므로 디코딩한다.
                    token = base64.b64decode(s.data["token"]).decode("utf-8")
                    print(f"Retrieved token for {sa_name}: {token[:10]}...")
                    return token
            except Exception as e:
                print(f"[DEBUG] Waiting for token in {secret_name}: {e}")
            time.sleep(1)

        print("Timeout waiting for token secret population.")
        return None

    def create_kubeconfig_secret(
        self,
        host_ns: str,
        playground_id: str,
        sandbox_ns: str,
        token: str,
    ) -> str:
        """
        파드 내 kubectl이 사용할 kubeconfig를 K8s Secret으로 생성한다.
        파드는 /kubeconfig/config에 이 Secret을 마운트하고 KUBECONFIG 환경변수 없이도 사용한다.

        kubeconfig 구조:
        - server: K8s API 서버 주소 (self.server_url, 클러스터 등록 시 kubeconfig에서 추출)
        - certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
          (파드 내 자동 마운트되는 클러스터 CA 인증서 경로)
        - token: sandbox 네임스페이스의 admin-sa JWT 토큰
        - namespace: sandbox_ns (kubectl의 기본 네임스페이스로 설정됨)

        [주의] certificate-authority 경로는 파드가 실행되는 클러스터와
        API 서버 클러스터가 동일한 경우에만 유효하다. 멀티클러스터에서
        파드와 API 서버가 다른 클러스터에 있으면 CA 검증이 실패할 수 있다.

        Returns:
            생성된 Secret 이름 (kubeconfig-{playground_id})
        """
        server_url = self.server_url
        if getattr(self, "ca_data", None):
            ca_section = f"    certificate-authority-data: {self.ca_data}"
        else:
            ca_section = "    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

        config_content = f"""apiVersion: v1
kind: Config
clusters:
- name: local
  cluster:
    server: {server_url}
{ca_section}
users:
- name: sandbox-user
  user:
    token: {token}
contexts:
- name: sandbox-context
  context:
    cluster: local
    namespace: {sandbox_ns}
    user: sandbox-user
current-context: sandbox-context
"""
        secret_name = f"kubeconfig-{playground_id}"
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                labels={
                    "app": "playground-kubeconfig",
                    "instance": playground_id,
                    "cluster": self.cluster_id,
                },
            ),
            # string_data: K8s가 자동으로 base64 인코딩하여 저장한다.
            string_data={"config": config_content},
            type="Opaque",
        )
        try:
            self.core_v1.create_namespaced_secret(namespace=host_ns, body=secret)
            return secret_name
        except client.exceptions.ApiException as e:
            if e.status == 409:
                return secret_name  # 이미 존재하면 멱등하게 성공 처리
            raise e

    def create_secret(
        self,
        namespace: str,
        playground_id: str,
        public_key: str,
        private_key: str = None,
    ) -> str:
        """
        SSH 키를 K8s Secret으로 저장한다.

        저장 내용:
        - authorized_keys: SSH 공개키. 파드의 /keys/authorized_keys에 마운트되어
                           sshd가 이 키로 접속을 인증한다.
        - private_key: SSH 개인키. 파드 내에서는 사용하지 않지만,
                       관리자가 /admin/playgrounds/{id}/ssh 엔드포인트로
                       나중에 재조회할 수 있도록 보관한다.

        Returns:
            생성된 Secret 이름 (ssh-key-{playground_id})
        """
        secret_name = f"ssh-key-{playground_id}"
        data = {"authorized_keys": public_key}
        if private_key:
            data["private_key"] = private_key  # 관리자 재조회용으로 저장
        secret = client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=client.V1ObjectMeta(
                name=secret_name,
                labels={
                    "app": "playground-ssh-key",
                    "instance": playground_id,
                    "cluster": self.cluster_id,
                },
            ),
            string_data=data,
            type="Opaque",
        )
        try:
            self.core_v1.create_namespaced_secret(namespace=namespace, body=secret)
            return secret_name
        except client.exceptions.ApiException as e:
            if e.status == 409:
                return secret_name
            raise e

    def get_private_key(self, namespace: str, playground_id: str) -> str | None:
        """
        SSH 개인키를 Secret에서 복원한다.
        관리자 SSH 접속 정보 조회 엔드포인트에서 사용된다.
        K8s API는 Secret.data 값을 base64로 반환하므로 디코딩한다.

        Returns:
            PEM 형식 개인키 문자열 또는 None
        """
        secret_name = f"ssh-key-{playground_id}"
        try:
            secret = self.core_v1.read_namespaced_secret(
                name=secret_name, namespace=namespace
            )
            if secret.data and "private_key" in secret.data:
                return base64.b64decode(secret.data["private_key"]).decode("utf-8")
        except Exception:
            pass
        return None

    # ── Deployment / Service ──────────────────────────────────────

    def create_deployment(
        self,
        namespace: str,
        playground_id: str,
        secret_name: str,
        kubeconfig_secret_name: str,
    ) -> str:
        """
        플레이그라운드 파드(SSH 서버)를 Deployment로 생성한다.

        컨테이너 구성:
        - 이미지: RUNNER_IMAGE 환경변수 또는 your-docker-id/playground-runner:v4
        - runAsUser=0 (root): entrypoint.sh가 sshd를 시작하려면 root 권한이 필요하다.
          allowPrivilegeEscalation=false로 추가 권한 획득을 차단한다.
        - 리소스: requests 100m/128Mi, limits 1CPU/1Gi
        - 볼륨 마운트:
          /keys/authorized_keys: SSH 인증 공개키 (read-only)
          /kubeconfig/config: kubectl용 kubeconfig (read-only)

        레이블 {app, instance, cluster}:
        - instance: WebSocket에서 Pod를 찾을 때 사용 (label_selector)
        - cluster: 멀티클러스터 환경에서 다른 클러스터의 리소스와 구분할 때 사용

        Returns:
            생성된 Deployment 이름 (ubuntu-sshd-{playground_id})
        """
        deployment_name = f"ubuntu-sshd-{playground_id}"
        labels = {
            "app": "playground",
            "instance": playground_id,
            "cluster": self.cluster_id,
        }

        # RUNNER_IMAGE 환경변수로 이미지 버전을 외부에서 주입할 수 있다.
        image_name = os.environ.get(
            "RUNNER_IMAGE", "your-docker-id/playground-runner:v4"
        )

        container = client.V1Container(
            name="ubuntu",
            image=image_name,
            image_pull_policy="Always",  # 항상 최신 이미지를 Pull하여 구버전 캐시 사용을 방지
            ports=[client.V1ContainerPort(container_port=2222, name="ssh")],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "100m", "memory": "128Mi"},
                limits={"cpu": "1", "memory": "1Gi"},
            ),
            security_context=client.V1SecurityContext(
                run_as_user=0,  # entrypoint.sh의 sshd 기동에 root 필요
                allow_privilege_escalation=False,  # 컨테이너 내에서 추가 권한 획득 불가
            ),
            env=[client.V1EnvVar(name="PLAYGROUND_ID", value=playground_id)],
            volume_mounts=[
                client.V1VolumeMount(name="keys", mount_path="/keys", read_only=True),
                client.V1VolumeMount(
                    name="kubeconfig", mount_path="/kubeconfig", read_only=True
                ),
            ],
        )

        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels=labels),
            spec=client.V1PodSpec(
                # playground-sa: 파드 신원(K8s RBAC)을 위한 SA.
                # 실제 kubectl 접근 권한은 kubeconfig Secret의 토큰이 담당한다.
                service_account_name="playground-sa",
                containers=[container],
                volumes=[
                    client.V1Volume(
                        name="keys",
                        secret=client.V1SecretVolumeSource(
                            secret_name=secret_name,
                            items=[
                                client.V1KeyToPath(
                                    key="authorized_keys", path="authorized_keys"
                                )
                            ],
                        ),
                    ),
                    client.V1Volume(
                        name="kubeconfig",
                        secret=client.V1SecretVolumeSource(
                            secret_name=kubeconfig_secret_name,
                            items=[client.V1KeyToPath(key="config", path="config")],
                        ),
                    ),
                ],
            ),
        )

        deployment = client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=client.V1ObjectMeta(name=deployment_name, labels=labels),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels=labels),
                template=template,
            ),
        )

        try:
            self.apps_v1.create_namespaced_deployment(
                namespace=namespace, body=deployment
            )
            return deployment_name
        except client.exceptions.ApiException as e:
            if e.status == 409:
                return deployment_name
            raise e

    def create_service(
        self,
        namespace: str,
        playground_id: str,
        annotations: dict = None,
    ) -> str:
        """
        플레이그라운드 SSH 접속을 위한 NodePort Service를 생성한다.
        포트 번호는 Kubernetes가 30000~32767 범위에서 자동 할당한다.

        annotations 매개변수:
        커스텀 플레이그라운드의 경우 허용된 네임스페이스/동사 정보를
        Service 어노테이션에 저장하여 목록 조회 시 RBAC 현황을 표시한다.

        Returns:
            생성된 Service 이름 (ubuntu-sshd-svc-{playground_id})
        """
        service_name = f"ubuntu-sshd-svc-{playground_id}"
        labels = {
            "app": "playground",
            "instance": playground_id,
            "cluster": self.cluster_id,
        }
        metadata = client.V1ObjectMeta(name=service_name, labels=labels)
        if annotations:
            metadata.annotations = annotations

        service = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=metadata,
            spec=client.V1ServiceSpec(
                type="NodePort",  # 외부 SSH 클라이언트 직접 접속을 위해 NodePort 사용
                selector=labels,
                ports=[
                    client.V1ServicePort(
                        name="ssh", protocol="TCP", port=2222, target_port=2222
                    )
                ],
            ),
        )
        try:
            self.core_v1.create_namespaced_service(namespace=namespace, body=service)
            return service_name
        except client.exceptions.ApiException as e:
            if e.status == 409:
                return service_name
            raise e

    # ── 조회 ──────────────────────────────────────────────────────

    def get_service_node_port(
        self, namespace: str, service_name: str, max_retries: int = 5
    ) -> int:
        """
        Service의 NodePort 번호를 조회한다.
        Service 생성 직후에는 NodePort가 즉시 할당되지 않을 수 있으므로
        최대 5초간 재시도한다.

        Returns:
            NodePort 번호 (int)

        Raises:
            Exception: max_retries 내에 NodePort를 얻지 못한 경우
        """
        for _ in range(max_retries):
            try:
                service = self.core_v1.read_namespaced_service(
                    name=service_name, namespace=namespace
                )
                for port in service.spec.ports:
                    if port.name == "ssh" and port.node_port:
                        return port.node_port
            except client.exceptions.ApiException:
                pass
            time.sleep(1)
        raise Exception(f"Could not retrieve NodePort for service {service_name}")

    def get_pod_node_ip(
        self, namespace: str, playground_id: str, max_retries: int = 20
    ) -> str:
        """
        플레이그라운드 파드가 스케줄된 노드의 IP를 조회한다.
        파드 스케줄링에 시간이 걸릴 수 있으므로 최대 20초 재시도한다.

        IP 우선순위:
        1. InternalIP (클러스터 내부 IP, 대부분의 환경에서 사용 가능)
        2. ExternalIP (클라우드 환경에서 외부 노출 IP)

        Returns:
            노드 IP 문자열

        Raises:
            Exception: 파드가 스케줄되지 않거나 노드 IP를 찾지 못한 경우
        """
        label_selector = f"instance={playground_id}"
        for _ in range(max_retries):
            try:
                pods = self.core_v1.list_namespaced_pod(
                    namespace=namespace, label_selector=label_selector
                )
                if pods.items:
                    pod = pods.items[0]
                    if pod.spec.node_name:
                        node = self.core_v1.read_node(pod.spec.node_name)
                        for address in node.status.addresses:
                            if address.type == "InternalIP":
                                return address.address
                        for address in node.status.addresses:
                            if address.type == "ExternalIP":
                                return address.address
            except Exception as e:
                print(f"Error getting pod info: {e}")
            time.sleep(1)
        raise Exception(
            f"Pod for {playground_id} was not scheduled or Node IP could not be found."
        )

    def list_playgrounds(self, namespace: str) -> list[dict]:
        """
        이 클러스터에 속한 플레이그라운드 목록을 반환한다.
        Service의 cluster 레이블로 필터링하여 멀티클러스터 환경에서 다른 클러스터의
        리소스와 섞이지 않도록 한다.

        RBAC 현황(허용 네임스페이스/동사)은 Service 어노테이션에서 읽어온다.
        """
        playgrounds = []
        try:
            svcs = self.core_v1.list_namespaced_service(
                namespace=namespace, label_selector="app=playground"
            )
            for svc in svcs.items:
                svc_cluster = svc.metadata.labels.get("cluster")

                # 다른 클러스터 ID가 붙은 Service는 건너뛴다 (멀티클러스터 혼재 방지)
                if svc_cluster and svc_cluster != self.cluster_id:
                    continue
                # cluster 레이블이 없는 레거시 리소스는 "default" 클러스터 소속으로 처리
                if not svc_cluster and self.cluster_id != "default":
                    continue

                instance_id = svc.metadata.labels.get("instance")
                created_at = svc.metadata.creation_timestamp
                svc_annotations = svc.metadata.annotations or {}

                playgrounds.append(
                    {
                        "id": instance_id,
                        "created_at": created_at.isoformat() if created_at else None,
                        # 어노테이션이 없으면 기본값 표시 (일반 플레이그라운드)
                        "namespaces": svc_annotations.get(
                            "playground.namespaces", "sandbox-only"
                        ),
                        "verbs": svc_annotations.get("playground.verbs", "*"),
                    }
                )
        except Exception as e:
            print(f"Error listing playgrounds: {e}")
        return playgrounds

    # ── 정리 / 삭제 ──────────────────────────────────────────────

    def cleanup_expired_playgrounds(
        self, namespace: str, max_age_seconds: int = 86400
    ) -> int:
        """
        생성 후 max_age_seconds(기본 86400초 = 24시간)가 지난 플레이그라운드를 삭제한다.

        삭제 범위:
        - study 네임스페이스의 Deployment, Service, Secret
        - sandbox-{id} 네임스페이스 전체 (delete_playground 내부에서 처리)

        Returns:
            이번 호출에서 삭제된 플레이그라운드 수 (고유 인스턴스 기준)
        """
        deleted_instances = set()
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            
            # 1. 서비스 기반 찾기 (정상적으로 프로비저닝 완료된 인스턴스)
            svcs = self.core_v1.list_namespaced_service(
                namespace=namespace, label_selector="app=playground"
            )
            for svc in svcs.items:
                svc_cluster = svc.metadata.labels.get("cluster")
                if svc_cluster and svc_cluster != self.cluster_id:
                    continue
                if not svc_cluster and self.cluster_id != "default":
                    continue

                created_at = svc.metadata.creation_timestamp
                if created_at:
                    age = (now - created_at).total_seconds()
                    if age > max_age_seconds:
                        instance_id = svc.metadata.labels.get("instance")
                        if instance_id and instance_id not in deleted_instances:
                            print(
                                f"Playground {instance_id} on {self.cluster_id} "
                                f"expired (Age: {age:.0f}s). Deleting..."
                            )
                            self.delete_playground(namespace, instance_id)
                            deleted_instances.add(instance_id)

            # 2. 고아 시크릿 기반 찾기 (생성 중 실패하여 Service가 없는 리소스, 예: kubeconfig-*, ssh-key-*)
            secrets = self.core_v1.list_namespaced_secret(namespace=namespace)
            for s in secrets.items:
                name = s.metadata.name
                if name.startswith("kubeconfig-") or name.startswith("ssh-key-"):
                    s_cluster = s.metadata.labels.get("cluster") if s.metadata.labels else None
                    if s_cluster and s_cluster != self.cluster_id:
                        continue
                    if not s_cluster and self.cluster_id != "default":
                        continue

                    created_at = s.metadata.creation_timestamp
                    if created_at:
                        age = (now - created_at).total_seconds()
                        if age > max_age_seconds:
                            if name.startswith("kubeconfig-"):
                                instance_id = name.replace("kubeconfig-", "", 1)
                            else:
                                instance_id = name.replace("ssh-key-", "", 1)

                            if instance_id and instance_id not in deleted_instances:
                                print(
                                    f"Orphaned Secret {name} on {self.cluster_id} "
                                    f"expired (Age: {age:.0f}s). Deleting entire playground..."
                                )
                                self.delete_playground(namespace, instance_id)
                                deleted_instances.add(instance_id)

        except Exception as e:
            print(f"Error during cleanup: {e}")
        return len(deleted_instances)

    def delete_custom_rbac(self, playground_id: str):
        """
        커스텀 플레이그라운드의 ClusterRole/ClusterRoleBinding 및
        네임스페이스 스코프 Role/RoleBinding을 레이블 셀렉터로 일괄 삭제한다.

        instance={playground_id},cluster={self.cluster_id} 레이블이 붙은 모든 RBAC 리소스를 삭제한다.
        레이블이 없는 기본 RBAC(admin-rb, admin-sa)은 sandbox 네임스페이스 삭제로 함께 정리된다.
        """
        custom_labels = f"instance={playground_id},cluster={self.cluster_id}"
        try:
            # 클러스터 수준 RBAC 삭제 (namespaces=["*"] 케이스)
            crs = self.rbac_v1.list_cluster_role(label_selector=custom_labels)
            for cr in crs.items:
                self.rbac_v1.delete_cluster_role(name=cr.metadata.name)

            crbs = self.rbac_v1.list_cluster_role_binding(label_selector=custom_labels)
            for crb in crbs.items:
                self.rbac_v1.delete_cluster_role_binding(name=crb.metadata.name)

            # 네임스페이스 수준 RBAC 삭제 (특정 네임스페이스 지정 케이스)
            roles = self.rbac_v1.list_role_for_all_namespaces(
                label_selector=custom_labels
            )
            for r in roles.items:
                self.rbac_v1.delete_namespaced_role(
                    name=r.metadata.name, namespace=r.metadata.namespace
                )

            rbs = self.rbac_v1.list_role_binding_for_all_namespaces(
                label_selector=custom_labels
            )
            for rb in rbs.items:
                self.rbac_v1.delete_namespaced_role_binding(
                    name=rb.metadata.name, namespace=rb.metadata.namespace
                )
        except Exception as e:
            print(f"Error deleting custom RBAC resources: {e}")

    def update_custom_rbac(
        self,
        namespace: str,
        playground_id: str,
        sandbox_ns: str,
        namespaces: list[str],
        verbs: list[str],
    ):
        """
        실행 중인 플레이그라운드의 RBAC을 교체한다.
        기존 커스텀 RBAC을 모두 삭제한 뒤 새 설정으로 재생성한다.
        파드 재시작 없이 즉시 적용된다(K8s RBAC은 실시간 반영).
        새 RBAC 설정을 Service 어노테이션에도 기록하여 목록 조회 시 반영한다.
        """
        # 기존 커스텀 RBAC 전부 제거 후 재생성
        self.delete_custom_rbac(playground_id)
        self.setup_custom_rbac(sandbox_ns, namespaces, verbs, playground_id)

        # Service 어노테이션 업데이트 (admin 대시보드 목록에 변경 사항 반영)
        service_name = f"ubuntu-sshd-svc-{playground_id}"
        annotations = {
            "playground.namespaces": ",".join(namespaces),
            "playground.verbs": ",".join(verbs),
        }
        try:
            self.core_v1.patch_namespaced_service(
                name=service_name,
                namespace=namespace,
                body={"metadata": {"annotations": annotations}},
            )
        except Exception as e:
            print(f"Error patching service annotations: {e}")

    def delete_playground(self, namespace: str, playground_id: str):
        """
        플레이그라운드와 관련된 모든 리소스를 삭제한다.
        대상: study 네임스페이스의 리소스 및 sandbox 네임스페이스 전체.
        """
        label_selector = f"instance={playground_id}"

        # 1. sandbox 네임스페이스 삭제 (내부의 모든 자원이 함께 정리됨)
        try:
            sandbox_ns = f"sandbox-{playground_id}"
            self.core_v1.delete_namespace(name=sandbox_ns)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                print(f"Error deleting sandbox namespace: {e}")

        # 2. Service 삭제 (study 네임스페이스)
        try:
            services = self.core_v1.list_namespaced_service(
                namespace=namespace, label_selector=label_selector
            )
            for svc in services.items:
                self.core_v1.delete_namespaced_service(
                    name=svc.metadata.name, namespace=namespace
                )
        except Exception as e:
            print(f"Error deleting service: {e}")

        # 3. Deployment 삭제 (study 네임스페이스)
        try:
            self.apps_v1.delete_collection_namespaced_deployment(
                namespace=namespace, label_selector=label_selector
            )
        except Exception as e:
            print(f"Error deleting deployment: {e}")

        # 4. 커스텀 ClusterRole/RoleBinding 삭제 (레이블 기반)
        self.delete_custom_rbac(playground_id)

        # 5. SSH 키 Secret 삭제 (study 네임스페이스)
        try:
            self.core_v1.delete_namespaced_secret(
                name=f"ssh-key-{playground_id}", namespace=namespace
            )
        except client.exceptions.ApiException as e:
            if e.status != 404:
                print(f"Error deleting secret: {e}")

        # 6. kubeconfig Secret 삭제 (study 네임스페이스)
        try:
            self.core_v1.delete_namespaced_secret(
                name=f"kubeconfig-{playground_id}", namespace=namespace
            )
        except client.exceptions.ApiException as e:
            if e.status != 404:
                print(f"Error deleting kubeconfig secret: {e}")
