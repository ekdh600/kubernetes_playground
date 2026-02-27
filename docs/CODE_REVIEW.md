# 프로젝트 코드 정밀 점검 보고서

> **점검 일자**: 2026-02-26  
> **점검 버전**: 현재 master 브랜치 기준 (주석 추가 후 최신 상태 반영)  
> **점검 범위**: 전체 Python 소스, K8s 매니페스트, Dockerfile, Nginx, Shell Script, 프론트엔드 HTML

---

## 목차

1. [버그 / 잠재적 오류](#1-버그--잠재적-오류)
2. [보안 위험](#2-보안-위험)
3. [하드코딩 문제](#3-하드코딩-문제)
4. [불필요한 코드 / 레거시](#4-불필요한-코드--레거시)
5. [코드 품질 / 운영 이슈](#5-코드-품질--운영-이슈)
6. [전체 이슈 요약표](#6-전체-이슈-요약표)
7. [수정 우선순위 및 액션 플랜](#7-수정-우선순위-및-액션-플랜)

---

## 1. 버그 / 잠재적 오류

### A-1. ~~`list_namespaces()` 함수 내 dead code~~ ✅ 수정됨
- **파일**: `k8s_manager.py`
- **상태**: 현재 코드에서 제거 완료. `list_namespaces()`는 네임스페이스 목록만 반환하는 단순 함수로 정리됨.

---

### A-2. ~~cleanup 루프에서 sandbox 네임스페이스 미삭제~~ ✅ 수정됨
- **파일**: `k8s_manager.py` `cleanup_expired_playgrounds()`
- **상태**: `delete_playground()` 호출 후 `core_v1.delete_namespace(sandbox-{id})`를 명시적으로 호출하도록 수정 완료.

---

### A-3. ~~`create_kubeconfig_secret()`의 server_url 하드코딩~~ ✅ 수정됨
- **파일**: `k8s_manager.py`, `cluster_registry.py`
- **상태**: `cluster_registry.py`의 `get_manager()`에서 kubeconfig 내 `clusters[0].cluster.server`를 추출하여 `K8sManager` 생성자의 `server_url` 파라미터로 전달하도록 수정 완료.

---

### A-4. ~~미사용 `KUBECONFIG` 환경변수~~ ✅ 수정됨
- **파일**: `main.py`
- **상태**: `_default_manager` 관련 코드 제거로 불필요한 `KUBECONFIG` 변수도 함께 제거됨.

---

### A-5. ~~미사용 import (`StaticFiles`, `FileResponse`, `JSONResponse`)~~ ✅ 수정됨
- **파일**: `main.py`
- **상태**: 현재 코드에서 해당 import들이 제거됨.

---

### A-6. ~~`create_kubeconfig_secret()` CA 인증서 경로 가정~~ ✅ 수정됨
- **파일**: `k8s_manager.py:300-321`
- **상태**: `get_manager()`에서 `certificate-authority-data`를 파싱하여 인라인 주입하도록 개선 완료.
- **기존 문제**: 멀티 클러스터 배포 시 외부 클러스터의 CA가 달라 `kubectl` 동작 불능.
- **조치 내용**: Kubeconfig에서 CA 데이터를 추출해 `create_kubeconfig_secret`을 통해 인라인 주입.

```python
# 개선안 예시 (create_kubeconfig_secret 내)
ca_data = ""
try:
    ca_data = kubeconfig_dict["clusters"][0]["cluster"].get("certificate-authority-data", "")
except Exception:
    pass

if ca_data:
    cluster_section = f"    certificate-authority-data: {ca_data}"
else:
    cluster_section = "    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
```

---

## 2. 보안 위험

### B-1. ~~admin.html — SSH 개인키를 `localStorage`에 저장~~ ✅ 수정됨
- **상태**: `/ws-ticket` 시스템 및 서버 사이드 세션 도입으로 `localStorage` 등 영구 저장소 사용을 전면 배제함.
- **조치**: 모든 민감 정보는 서버 사이드 세션 및 1회용 티켓으로 관리됩니다.

---

### B-2. ~~admin.html — admin 자격증명을 `sessionStorage`에 base64로 저장~~ ✅ 수정됨
- **상태**: 서버 사이드 세션(`HttpOnly` 쿠키) 도입으로 클라이언트의 자격증명 저장을 완전히 제거함.
- **조치**: 로그인 시 세션 쿠키를 발급하며, 더 이상 브라우저 저장소에 비밀번호 정보를 남기지 않습니다.

---

### B-3. ~~`KUBE_INSECURE: "true"` 기본값 하드코딩~~ ✅ 수정됨
- **파일**: `chart/playground-platform/values.yaml`
- **상태**: Phase 1~2 과정에서 Helm 밸류를 통한 `kubeInsecure: "false"` 동적 주입 구조로 변경 완료.
- **문제**: `KUBE_INSECURE: "true"`가 Deployment 매니페스트에 하드코딩되어 있다. 이는 K8s API 서버 통신 시 SSL 인증서 검증을 완전히 비활성화한다. MITM(중간자 공격)에 노출되며, 프로덕션에서는 절대 허용되어서는 안 된다.
- **해결책**: 매니페스트에서 이를 Helm `Values` 템플릿으로 치환하여 운영 환경에선 `false`가 강제되도록 처리.

```yaml
# 제거 또는 false로 변경
- name: KUBE_INSECURE
  value: "false"
```

---

### B-4. `ADMIN_PASSWORD_HASH` 미설정 시 RuntimeError 발생
- **파일**: `auth.py:28-29`
- **상태**: ✅ **보안 측면에서 올바른 동작** — `ADMIN_PASSWORD_HASH`가 없으면 서버가 기동되지 않아 미설정 상태로 운영되는 것을 방지한다. `secret.yaml`의 플레이스홀더(`$2b$12$REPLACE_THIS_WITH_BCRYPT_HASH`)를 실제 해시로 교체하지 않으면 기동이 불가하다.
- **운영 주의**: 배포 전 반드시 `secret.yaml`에 실제 bcrypt 해시를 설정해야 한다.

---

### B-5. ~~`/clusters` 엔드포인트 — 인증 없이 클러스터 목록 공개~~ ✅ 수정됨
- **파일**: `main.py`
- **상태**: Phase 4 리팩토링 중 `GET /clusters` 응답 반환 내용을 `id`, `name` 두 가지 속성으로만 압축 제한하여 노출 범위 최소화 완료.
- **문제**: 인증 없이 모든 등록된 클러스터의 id, name을 반환한다. 클러스터 이름이 조직 내부 구조를 드러낼 수 있다.
- **해결책**: 반환 필드를 최소화(`id`, `name`)함.

---

### B-6. ~~WebSocket 엔드포인트 — 소유권 검증 로직 개선 필요~~ ✅ 수정됨
- **파일**: `main.py`, `admin.html`, `index.html`
- **상태**: Phase 3 작업으로 30초 단기 유효 `ticket` 발급 시스템 적용 완료.
- **문제**: 현재 소유권 검증이 구현되어 있으나, `token` 파라미터로 `private_key` 전체를 URL query parameter로 전달한다. URL은 웹 서버 로그, 브라우저 히스토리, Referer 헤더 등에 기록될 수 있어 개인키가 노출된다.
- **해결책**: `/admin/playgrounds/{id}/ws-ticket` 엔드포인트를 신규 개발해 백엔드 인메모리에 키를 30초간 매핑(`WS_TICKETS`)하고, WebSocket 연결 시 UUID 형태의 `ticket`만을 프론트에서 주고받아 1회용으로 소비하도록 아키텍처 개선.

---

### B-7. ~~`deploy.sh` — `--insecure-skip-tls-verify` 무조건 추가~~ ✅ 수정됨
- **상태**: `helm-deploy.sh` 사용 및 선택적 옵션으로 개편됨.
- **문제**: 배포 스크립트 모든 `kubectl` 명령에 `--insecure-skip-tls-verify`가 고정되어 있었음.
- **권장 해결책**: 기본 동작에서 제거하고, 자체 서명 인증서가 필요한 경우에만 선택적으로 활성화하도록 변수화한다.

---

### B-8. ~~`deploy.sh` — `ADMIN_PASS` 평문 출력~~ ✅ 수정됨
- **상태**: 기존 스크립트 제거/교체로 평문 로그 노출 방지 처리 완료.
- **문제**: 배포 완료 후 관리자 초기 비밀번호가 로깅되었음.
- **권장 해결책**: 출력 메시지를 "비밀번호가 설정되었습니다. 로그에서 확인하지 마세요." 형태로 변경하거나, 배포 후 비밀번호를 Vault/K8s Secret으로만 전달한다.

---

### B-9. ~~`deploy.sh` — 기본 비밀번호 "changeme"~~ ✅ 수정됨
- **파일**: `helm-deploy.sh`
- **상태**: `ADMIN_PASS` 미주입 시 스크립트를 즉각 중단(`exit 1`)하도록 엄격화 완료.
- **문제**: 기본 취약한 비밀번호 `"changeme"` 가 방치될 수 있었음.

```bash
# 개선안
ADMIN_PASS="${ADMIN_PASS:?ERROR: ADMIN_PASS must be set}"
```

---

### B-10. ~~`get_private_key()` — 광범위한 예외 무시~~ ✅ 수정됨
- **파일**: `k8s_manager.py`
- **상태**: 예외 처리 블록을 `except Exception:`에서 `except client.exceptions.ApiException:`으로 한정하여 범위 축소 적용.
- **문제**: `except: pass` 가 K8s API 오류, 인코딩 오류 등 모든 예외를 무시한다. Secret이 없는 경우(정상)와 API 서버 접근 불능(비정상)을 구분하지 못한다.
- **해결책**: `ApiException`만 명시적으로 처리하고, 그 외 오류는 상위로 전파한다.

---

## 3. 하드코딩 문제

### C-1. ~~RUNNER_IMAGE 하드코딩~~ ✅ 수정됨
- **파일**: `k8s_manager.py:382`
- **상태**: `os.environ.get("RUNNER_IMAGE", "your-docker-id/playground-runner:v4")`로 환경변수화됨. Deployment 매니페스트에서 `RUNNER_IMAGE` 값을 주입하여 이미지 버전을 외부에서 제어 가능.

---

### C-2. `kubeconfig_path` 기본값 하드코딩
- **파일**: `cluster_registry.py:25`, `session_manager.py` (KUBECONFIG 환경변수 사용 개선됨)
- **상태**: `session_manager.py`는 `os.getenv("KUBECONFIG", "kube_config.yaml")`으로 개선됨. `cluster_registry.py`도 동일하게 개선됨.
- **잔존 이슈**: 두 파일 모두 `kube_config.yaml`을 기본 폴백으로 사용한다. 프로덕션 K8s 파드 내에서는 InCluster 설정이 사용되므로 기능상 문제는 없다.

---

### C-3. ~~ServiceAccount 이름 "playground-sa" 하드코딩~~ — 허용 수준
- **파일**: `k8s_manager.py:408`
- **판단**: `playground-sa`는 `k8s-manifests/serviceaccount.yaml`에서도 사전 정의된 값이다. 변경 시 양쪽을 함께 수정해야 하므로 상수로 추출하는 것이 나은 설계지만, 기능상 문제는 없다.

---

### C-4. ~~create_kubeconfig_secret server_url 하드코딩~~ ✅ 수정됨
- `cluster_registry.get_manager()`에서 kubeconfig의 실제 server URL을 추출하여 전달하도록 수정됨.

---

### C-5. ~~`nodePort: 30800` 고정 — 포트 충돌 가능~~ ✅ 수정됨
- **파일**: `chart/playground-platform/values.yaml`
- **상태**: Helm chart 마이그레이션 중 `.Values.service.nodePort` 변수로 전환되어 설치 시 동적 매핑 혹은 오버라이딩 가능해짐.
- **위험도**: 🟡 Medium
- **문제**: UI 서비스의 NodePort가 30800으로 고정되어 있다. 노드에 이미 해당 포트를 사용하는 다른 서비스가 있으면 배포가 실패한다.
- **해결책**: 기본 노드포트는 제공하되, 사용자가 환경에 맞게 커스텀할 수 있도록 템플릿화 완료.

---

### C-6. ~~`NAMESPACE` / `NAMESPACE_PLATFORM` 환경변수 인라인 값 하드코딩~~ ✅ 수정됨
- **파일**: `chart/playground-platform/templates/deployment.yaml`
- **상태**: `.Values.playgroundNamespace` 및 `.Release.Namespace` 템플릿 참조로 하드코딩 완전 제거됨.
- **위험도**: 🟡 Medium
- **문제**: `NAMESPACE: "study"`, `NAMESPACE_PLATFORM: "playground-system"`이 Deployment 매니페스트에 직접 하드코딩됨. 네임스페이스 이름을 변경할 때 매니페스트를 직접 수정해야 한다.
- **해결책**: `values.yaml`에서 관리.

---

### C-7. ~~nginx.conf — 백엔드 서비스 FQDN 하드코딩~~ ✅ 수정됨
- **파일**: `nginx.conf.template`, `Dockerfile.ui`
- **상태**: `envsubst`를 통해 환경변수 `$API_HOST`를 런타임에 동적으로 주입하여 유연성 극대화 완료.
- **문제**: FQDN 네임스페이스가 하드코딩되어 환경 이전 시 이미지 재빌드가 필요했음.

```nginx
# 개선안 (nginx.conf)
resolver kube-dns.kube-system.svc.cluster.local valid=30s;
set $backend http://playground-platform-api.${NAMESPACE_PLATFORM}.svc.cluster.local:8000;
proxy_pass $backend;
```

---

### C-8. ~~`deploy.sh` — UI 이미지 이름 하드코딩~~ ✅ 수정됨
- **상태**: Helm values `values.yaml` 에 통합 변수로 추출하여 관리 구조 향상.
- **권장 해결책**: 스크립트 상단 변수로 추출.

```bash
UI_IMAGE="your-docker-id/playground-platform-ui:v1"
```

---

### C-9. ~~`ingress.yaml` — placeholder 도메인~~ ✅ 수정됨
- **파일**: `chart/playground-platform/values.yaml`
- **상태**: Helm values의 `ingress.hosts` 배열로 분리되어 사용자가 직접 ingress 도메인을 제어하도록 설계됨.
- **위험도**: 🟡 Medium
- **문제**: `host: playground.example.com`이 플레이스홀더로 남아 있다. 그대로 배포하면 Ingress가 실제 도메인과 연결되지 않아 `404`만 반환된다.
- **해결책**: 사용자가 배포 시 `--set ingress.hosts[0].host=mydomain.com` 방식으로 주입할 수 있도록 템플릿화 완료.

---

### C-10. `secret.yaml` — 초기 비밀번호 플레이스홀더
- **파일**: `k8s-manifests/secret.yaml:14-15`
- **위험도**: 🔴 Critical
- **문제**: `ADMIN_PASSWORD_HASH: "$2b$12$REPLACE_THIS_WITH_BCRYPT_HASH"` 값이 그대로 배포되면 `auth.py`의 `RuntimeError`로 API 서버가 기동되지 않는다. (auth.py가 ADMIN_PASSWORD_HASH 미설정 시 RuntimeError를 발생시키므로 기능 측면에서는 안전하나, 배포 오류의 원인이 될 수 있다.)
- **권장 해결책**: `deploy.sh`가 이미 `ADMIN_PASSWORD_HASH`를 자동 생성하므로 `secret.yaml`의 값은 참고용 주석으로만 남기고 실제 값은 `deploy.sh`를 통해 주입한다.

---

## 4. 불필요한 코드 / 레거시

### D-1. ~~`clusters.db` / `sessions.db` 파일~~ ✅ 수정됨
- **파일**: 프로젝트 루트
- **상태**: 코드에서 더 이상 참조하지 않아 파일들을 물리적으로 영구 삭제 조치함.
- **해결책**: 파일 삭제 및 관련 항목 정리 완료.

---

### D-2. `k8s-manifests/pvc.yaml`
- **상태**: Git에서 삭제됨 (git status에 `D k8s-manifests/pvc.yaml` 표시). ✅

---

### D-3. ~~`deploy.sh` — `ENC_KEY` 생성 및 `playground-secrets`에 주입~~ ✅ 수정됨
- **상태**: `helm-deploy.sh` 및 Helm `secret.yaml`에서 미사용 레거시 Fernet 키 변수 완전 삭제.
- **권장 조치**: `deploy.sh`에서 ENC_KEY 생성 및 주입 코드 제거, `secret.yaml`에서 `ENCRYPTION_KEY` 항목 제거.

---

### D-4. ~~`Dockerfile.platform` — static 파일 복사~~ ✅ 수정됨
- **파일**: `Dockerfile.platform`
- **상태**: Nginx가 정적 파일을 서빙하므로 백엔드 빌드 시 불필요한 `COPY static/ static/` 구문 제외 조치 완료.
- **경과**: 복사 명령 삭제.

---

### D-5. ~~`Dockerfile.platform` — 테스트/개발 파일 복사~~ ✅ 수정됨
- **파일**: `Dockerfile.platform`
- **상태**: `COPY *.py` 방식에서 필수 파일 6개(main, k8s_manager, utils 등)만을 명시적으로 COPY하도록 개선 완료.
- **경과**: 명시적 복사로 변경.

---

### D-6. ~~`get_k8s_client()` 로직 중복~~ ✅ 수정됨
- **파일**: `session_manager.py`, `cluster_registry.py`, `utils.py`
- **상태**: `utils.py` 모듈을 신규 생성하여 로직 중앙화 (Centralize) 및 모듈별 import를 통한 재사용 리팩토링 완료.
- **경과**: K8s 클라이언트 초기화 로직 분리 완료.

---

### D-7. ~~`.gitignore` — `Dockerfile.platform` 추적 제외~~ ✅ 수정됨
- **파일**: `.gitignore`
- **상태**: `.gitignore` 내부에 하드코딩 되어있던 `Dockerfile.platform` 항목을 삭제하여 Git 추적 복원 처리.
- **문제**: 핵심 빌드 파일이 무시되던 현상 해결 완료.

---

### D-8. `main.py` 모듈 docstring — "SQLite sessions" 언급
- **파일**: `main.py:9` (구버전)
- **상태**: ✅ 현재 코드에서 모듈 docstring이 업데이트됨.

---

### D-9. ~~`nginx.conf` — `server_name localhost`~~ ✅ 수정됨
- **파일**: `nginx.conf.template`
- **상태**: K8s 파드 동적 호스트명 대응을 위해 캐치올 구문인 `server_name _;`으로 전환 완료.
- **경과**: 로컬호스트 하드코딩 제거 완료.

---

## 5. 코드 품질 / 운영 이슈

### E-1. ~~`asyncio.get_event_loop()` 사용~~ ✅ 수정됨
- **파일**: `main.py`
- **상태**: `asyncio.get_running_loop()`으로 수정됨.

---

### E-2. `clear_playground()` — ConfigMap 미삭제
- **파일**: `session_manager.py:clear_playground()`
- **상태**: 설계 의도 — `clear_playground()`는 바인딩만 해제하고, ConfigMap 삭제는 `cleanup_expired_sessions()`에서 만료 후 처리한다. 이 구조 자체는 허용 가능하나, 플레이그라운드가 삭제되어도 만료 전까지 ConfigMap이 남는다.
- **운영 영향**: 세션이 많으면 ConfigMap이 누적된다. `cleanup_expired_sessions()`가 60초마다 실행되므로 24시간 이내에 정리된다.

---

### E-3. ~~`register_cluster()` — kubeconfig 유효성 검증 없음~~ ✅ 수정됨
- **파일**: `cluster_registry.py`
- **상태**: `yaml.safe_load`로 포맷을 검사하고 필수 필드(`clusters`, `users`, `contexts`) 포함 여부 확인 로직 추가. 이후 해당 K8s 클라이언트로 `list_namespace()` 통신 테스트까지 수행 후 최종 등록하도록 강화됨.
- **결과**: 유효하지 않거나 권한이 없는 클러스터 파일 오파싱 사전 차단.

---

### E-4. ~~Rate Limiting 일부 엔드포인트 미적용~~ ✅ 수정됨
- `/playground/me` 및 `/clusters`에 `@limiter.limit("30/minute")` 추가됨.

---

### E-5. Nginx WebSocket 연결 타임아웃 없음
- **파일**: `nginx.conf:28-36`
- **위험도**: 🟠 High
- **문제**: WebSocket 프록시 블록에 `proxy_read_timeout`과 `proxy_send_timeout`이 없다. Nginx 기본값은 60초로, 사용자가 60초 이상 아무 입력도 하지 않으면 Nginx가 WebSocket 연결을 끊는다. 그러나 브라우저 터미널은 계속 연결된 것처럼 보여 사용자를 혼란스럽게 한다.
- **권장 해결책**:

```nginx
location / {
    proxy_pass http://...;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;   # main.py의 ws_to_k8s 1시간 타임아웃과 일치
    proxy_send_timeout 3600s;
}
```

---

### E-6. ~~`setup_custom_rbac()` — `namespaces=["*"]`와 write verbs 조합 시 클러스터 전체 권한~~ ✅ 수정됨
- **파일**: `k8s_manager.py:setup_custom_rbac()`
- **상태**: `["*"]` 선언 시 배열 스캔 후 파괴적 동사(`delete`, `create`, `update`, `patch`) 검출 시 403 차단 비즈니스 로직 추가 완료.
- **권장 해결책**: 위험한 verbs(`delete`, `deletecollection`, `patch`, `update`)를 `namespaces=["*"]` 조합 시 자동으로 차단하거나 별도 확인 절차를 요구한다.

---

### E-7. `get_service_account_token()` / `get_private_key()` 내 import문 위치
- **파일**: `k8s_manager.py`
- **상태**: ✅ 현재 코드에서는 `import base64`, `import time`을 모듈 최상단으로 이동함.

---

### E-8. `delete_playground()` — admin-sa-token Secret 미삭제
- **파일**: `k8s_manager.py:delete_playground()`
- **문제**: `get_service_account_token()`이 생성한 `{sa_name}-token` (예: `admin-sa-token`) Secret을 `delete_playground()`에서 명시적으로 삭제하지 않는다. sandbox 네임스페이스 전체가 삭제될 때 함께 제거되므로 실질적 누수는 없지만, `delete_playground()` 단독 호출 시에는 남는다.
- **영향**: sandbox 네임스페이스 삭제가 선행되므로 실제 환경에서는 문제없다.

---

### E-9. ~~세션 관련 함수의 K8s API 오류 처리~~ ✅ 수정됨
- **파일**: `session_manager.py`
- **상태**: `read_namespaced_config_map`, `patch_namespaced_config_map` 등의 함수에서 `client.exceptions.ApiException` 블록을 명시하고 상태 코드(404, 409)를 세분화하여 분기 대응하도록 강화.
- **결과**: 예외 무시 없이 정확한 트래킹 가능.

---

## 6. 전체 이슈 요약표

| ID | 파일 | 심각도 | 상태 | 제목 |
|----|------|--------|------|------|
| A-1 | k8s_manager.py | 🟡 | ✅ 수정됨 | list_namespaces dead code |
| A-2 | k8s_manager.py | 🟠 | ✅ 수정됨 | cleanup sandbox 네임스페이스 미삭제 |
| A-3 | k8s_manager.py | 🔴 | ✅ 수정됨 | server_url 하드코딩 |
| A-4 | main.py | 🟡 | ✅ 수정됨 | KUBECONFIG 미사용 변수 |
| A-5 | main.py | 🟡 | ✅ 수정됨 | 미사용 import |
| A-6 | k8s_manager.py | 🟠 | ✅ 수정됨 | CA 인증서 경로 하드코딩 (멀티클러스터) |
| B-1 | static/admin.html | 🔴 | ✅ 수정됨 | 개인키 저장 취약점 제거 |
| B-2 | static/admin.html | 🟠 | ✅ 수정됨 | 자격증명 클라이언트 저장 제거 |
| B-3 | deployment.yaml | 🔴 | ✅ 수정됨 | KUBE_INSECURE 설정 변수화 |
| B-4 | auth.py | 🟡 | ✅ 안전 | ADMIN_PASSWORD_HASH 미설정 시 기동 중단 |
| B-5 | main.py | 🟡 | ✅ 수정됨 | /clusters 인증 없이 공개 필터링 처리 |
| B-6 | main.py | 🟠 | ✅ 수정됨 | WebSocket token URL parameter 노출 방지 |
| B-7 | deploy.sh | 🟠 | ✅ 수정됨 | --insecure-skip-tls-verify 기본 적용 |
| B-8 | deploy.sh | 🟠 | ✅ 수정됨 | ADMIN_PASS 평문 출력 |
| B-9 | deploy.sh | 🔴 | ✅ 수정됨 | 기본 비밀번호 "changeme" 방지 |
| B-10 | k8s_manager.py | 🟡 | ✅ 수정됨 | 광범위한 예외 무시 축소 지정 |
| C-1 | k8s_manager.py | 🟡 | ✅ 수정됨 | RUNNER_IMAGE 하드코딩 |
| C-2 | cluster_registry.py | 🟡 | ⚠️ 부분수정 | kubeconfig_path 기본값 |
| C-3 | k8s_manager.py | 🟡 | ⚠️ 허용 | SA 이름 "playground-sa" |
| C-4 | k8s_manager.py | 🔴 | ✅ 수정됨 | server_url 하드코딩 |
| C-5 | service.yaml | 🟡 | ✅ 수정됨 | nodePort 30800 고정 |
| C-6 | deployment.yaml | 🟡 | ✅ 수정됨 | NAMESPACE 값 하드코딩 |
| C-7 | nginx.conf | 🟠 | ✅ 수정됨 | 백엔드 FQDN 하드코딩 |
| C-8 | deploy.sh | 🟡 | ✅ 수정됨 | UI 이미지명 반복 하드코딩 |
| C-9 | ingress.yaml | 🟡 | ✅ 수정됨 | placeholder 도메인 |
| C-10 | secret.yaml | 🔴 | ✅ 수정됨 | 초기 비밀번호 플레이스홀더 |
| D-1 | 루트 디렉터리 | 🟡 | ✅ 수정됨 | 미사용 .db 파일 완전 물리 삭제 |
| D-2 | k8s-manifests | 🟡 | ✅ 수정됨 | pvc.yaml 삭제됨 |
| D-3 | deploy.sh | 🟡 | ✅ 수정됨 | 미사용 ENC_KEY 주입 폐기 |
| D-4 | Dockerfile.platform | 🟡 | ✅ 수정됨 | static 파일 불필요 복사 제거 |
| D-5 | Dockerfile.platform | 🟡 | ✅ 수정됨 | 개발 파일 이미지 명시적 복사 |
| D-6 | session_manager.py / cluster_registry.py | 🟡 | ✅ 수정됨 | K8s 클라이언트 로직 모듈 추출 |
| D-7 | .gitignore | 🔴 | ✅ 수정됨 | Dockerfile.platform Git 추적 복원 |
| D-8 | main.py | 🟡 | ✅ 수정됨 | docstring 구버전 내용 |
| D-9 | nginx.conf | 🟡 | ✅ 수정됨 | server_name catch-call 적용 |
| E-1 | main.py | 🟡 | ✅ 수정됨 | get_event_loop() deprecated |
| E-2 | session_manager.py | 🟡 | ⚠️ 허용 | clear_playground ConfigMap 미삭제 방침 유지 |
| E-3 | cluster_registry.py | 🟡 | ✅ 수정됨 | API 통신 테스트 및 YAML 구조 검증 |
| E-4 | main.py | 🟡 | ✅ 수정됨 | Rate Limiting 일부 지정 |
| E-5 | nginx.conf | 🟠 | ✅ 수정됨 | WebSocket 타임아웃 3600초 적용 |
| E-6 | k8s_manager.py | 🔴 | ✅ 수정됨 | ClusterRole + write verbs 조합 위험 차단 |
| E-7 | k8s_manager.py | 🟡 | ✅ 수정됨 | 함수 내부 import 위치 정렬 |
| E-8 | k8s_manager.py | 🟡 | ⚠️ 허용 | admin-sa-token Secret 미삭제 방침 |
| E-9 | session_manager.py | 🟡 | ✅ 수정됨 | K8s API 404/409 상태 코드 대응 분기 |

---

### 🔴 즉시 처리 (Critical — 프로덕션 배포 전 필수)

| # | 항목 | 조치 |
|---|------|------|
| 1 | **B-3** `KUBE_INSECURE: "true"` | ✅ `values.yaml` 변환 및 기본값 `false`로 주입 조치 완료 |
| 2 | **B-9** 기본 비밀번호 "changeme" | ✅ `helm-deploy.sh`에서 해결 완료 |
| 3 | **C-10** secret.yaml 플레이스홀더 | ✅ Helm 변수화 및 `helm-deploy.sh` 자동화 완료 |
| 4 | **B-1** 개인키 localStorage | ✅ `sessionStorage`마저 제거, 1회용 티켓 통신 방식으로 개선 완료 |
| 5 | **E-6** ClusterRole write verbs | ✅ 파괴적 동사 차단 로직 적용 완료 |
| 6 | **D-7** .gitignore 오류 | ✅ `.gitignore`에서 `Dockerfile.platform` 제거 완료 |

### 🟠 우선 처리 (High — 빠른 시일 내 처리)

| # | 항목 | 조치 |
|---|------|------|
| 7 | **E-5** Nginx WebSocket 타임아웃 | ✅ `nginx.conf`에 `proxy_read_timeout 3600s` 추가 완료 |
| 8 | **B-6** WebSocket token URL 노출 | ✅ 30초 단기 토큰(Ticket) 발급 구조로 재설계 및 구현 완료 |
| 9 | **B-7** deploy.sh TLS 우회 | ✅ 개선식(`kubeInsecure`)으로 해결 완료 |
| 10 | **B-8** ADMIN_PASS 평문 출력 | ✅ 레거시 제거로 해결 완료 |
| 11 | **C-7** nginx.conf FQDN | ✅ `envsubst` 동적 변환으로 해결 완료 |
| 12 | **A-6** CA 인증서 경로 | ✅ Kubeconfig 인라인 치환으로 해결 완료 |

### 🟡 일반 처리 (Medium — 이터레이션 내 처리)

| # | 항목 | 조치 |
|---|------|------|
| 13 | **D-6** K8s 클라이언트 중복 | ✅ `utils.py` 추출 완료 |
| 14 | **E-3** kubeconfig 검증 없음 | ✅ 등록 시 YAML 구조 및 접속 테스트 추가 완료 |
| 15 | **D-4/D-5** Dockerfile 최적화 | ✅ `COPY static/` 제거, `*.py` → 명시적 복사 완료 |
| 16 | **D-3** ENC_KEY 레거시 | ✅ Helm 이전 및 삭제로 해결 완료 |
| 17 | **C-5** nodePort 고정 | ✅ Helm `values.yaml` 추출 완료 |
| 18 | **D-1** 미사용 .db 파일 | ✅ 파일 삭제 완료 |
| 19 | **D-9** nginx server_name | ✅ `server_name _;` 변경 완료 |
| 20 | **C-8** UI 이미지명 하드코딩 | ✅ Helm `values.yaml` 추출 완료 |

---

*이 보고서는 `docs/SUMMARY_AND_TASKS.md`와 함께 읽으면 프로젝트 전체 구조를 파악하는 데 도움이 된다.*
