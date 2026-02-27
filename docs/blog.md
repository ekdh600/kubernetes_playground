# 브라우저로 Kubernetes 실습 환경을 만들어보았다 — Playground Platform 개발기

> (직접 개발한 프로젝트를 기반으로 작성하였으나, 명확하지 않은 부분은 경험을 토대로 작성하였습니다.  
> 이는 정확한 정보가 아닐 수 있음을 알려드립니다.)

## **공식 사이트**

광고 클릭은 큰 힘이 됩니다!

- [Kubernetes Python Client — GitHub](https://github.com/kubernetes-client/python)
- [xterm.js — 브라우저 터미널 라이브러리](https://xtermjs.org/)
- [FastAPI — Python 비동기 웹 프레임워크](https://fastapi.tiangolo.com/)

---

## **서론**

**사전 준비**

**• PRE:**

> Kubernetes 기초, Python, Docker, Helm 기본 지식

---

Kubernetes를 처음 배울 때 가장 번거로운 건 환경 세팅이다.

minikube를 설치하고, kubeconfig를 맞추고, kubectl을 설치하고... 그러다 보면 정작 배우고 싶었던 내용은 뒷전이 된 경험이 한 번씩은 있을 것이다.

그래서 만들었다. 버튼 하나 클릭하면 Kubernetes 실습 환경이 뚝딱 생기고, 브라우저 터미널에서 바로 `kubectl`을 쓸 수 있는 플랫폼을.

이름하여 **Kubernetes Playground Platform**이다.

---

## **왜 만들었나**

솔직히 처음에는 별거 아닌 줄 알았다.

*"파드 하나 띄우고 웹 터미널 연결하면 되지 않나?"*

그런데 막상 만들다 보니 생각보다 고려할 게 많았다.

- 사용자마다 격리된 환경이 필요하다.
- 브라우저 터미널과 Kubernetes Pod를 어떻게 연결하지?
- 세션은 어떻게 관리하지? DB를 따로 써야 하나?
- SSH 접속도 지원하고 싶다.
- 클러스터가 여러 개라면?

이 과정에서 나온 설계 결정들이 꽤 재미있었다.

---

## **전체 구조**

```
브라우저 (xterm.js)
    │
    ▼
Nginx UI 서버 (NodePort)
    │  역방향 프록시
    ▼
FastAPI API 서버 (ClusterIP)
    │
    ├── REST API → K8s API 서버 (플레이그라운드 생성/삭제)
    └── WebSocket → K8s Pod exec 스트림 (터미널 연결)
```

구조 자체는 단순하다.

API 서버가 Kubernetes의 Pod를 직접 만들고, 그 Pod에 WebSocket으로 연결한다.

사용자 입장에서는 버튼 하나만 누르면 된다.

---

## **재미있었던 포인트 1: WebSocket + kubernetes.stream**

가장 골머리를 앓은 부분이다.

xterm.js는 WebSocket 기반이다. 그런데 Kubernetes Pod에 붙는 `kubernetes.stream`은 **동기(blocking) 방식**이다.

이 둘을 어떻게 연결하느냐가 문제였다.

처음에는 단순히 WebSocket 이벤트 루프 안에서 `kubernetes.stream`을 같이 돌렸다.

> 결과? FastAPI 이벤트 루프가 완전히 블로킹됐다. 다른 API 요청이 전혀 처리되지 않았다.

그래서 나온 해결책이 `asyncio.to_thread()`다.

```python
async def k8s_to_ws(resp, ws):
    def _blocking_read():
        while resp.is_open():
            resp.update(timeout=0.1)
            if resp.peek_stdout():
                data = resp.read_stdout()
                # 이벤트 루프에 전송 요청
                asyncio.run_coroutine_threadsafe(
                    ws.send_text(data), loop
                )
    await asyncio.to_thread(_blocking_read)
```

`kubernetes.stream`의 블로킹 루프를 별도 스레드에서 돌리고, 데이터가 오면 `run_coroutine_threadsafe()`로 이벤트 루프에 콜백을 던지는 방식이다.

이 패턴으로 FastAPI 이벤트 루프를 막지 않으면서도 실시간 터미널 스트리밍이 가능해졌다.

---

## **재미있었던 포인트 2: DB 없이 세션 관리**

세션 데이터를 어디에 저장할지 고민했다.

처음에는 SQLite를 쓰려 했다. 그런데 생각해보니 이미 Kubernetes가 있는데 굳이 DB를 추가해야 하나 싶었다.

**답: Kubernetes ConfigMap을 세션 저장소로 쓴다.**

```yaml
# playground-session-{uuid} ConfigMap
data:
  created_at: "2026-01-01T00:00:00"
  expires_at: "2026-01-02T00:00:00"
  playground_id: "ab3c7d91"
  cluster_id: "f2e8a1b3"
```

이렇게 하면 장점이 여럿이다.

- API 서버를 재시작해도 세션이 살아있다 (stateless 서버)
- `kubectl get configmap -l type=session` 으로 실시간 현황 확인 가능
- 외부 DB가 없으니 운영 복잡도가 줄어든다

마찬가지로 클러스터 kubeconfig도 **Kubernetes Secret에** 저장한다. 별도의 암호화 저장소 없이 K8s etcd 암호화에 의존하는 방식이다.

> 물론 ConfigMap은 기본적으로 etcd 암호화 대상이 아니라는 점은 알고 있어야 한다.  
> 세션 데이터 자체는 민감하지 않지만, 운영 환경이라면 etcd 암호화 설정을 권장한다.

---

## **재미있었던 포인트 3: 세션별 SSH 키 쌍**

SSH 접속을 지원하고 싶었다.

브라우저 터미널은 편리하지만 결국 로컬 터미널이 더 쾌적하다. 그래서 NodePort 서비스로 SSH 직접 접속도 가능하게 만들었다.

그런데 SSH 비밀번호를 어떻게 관리하지? 정적인 비밀번호는 공유될 위험이 있다.

**답: 플레이그라운드마다 새 RSA 4096-bit 키 쌍을 동적으로 생성한다.**

```python
from cryptography.hazmat.primitives.asymmetric import rsa

private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=4096,
)
```

공개키는 파드 내부의 `authorized_keys`에 들어가고, 개인키는 플레이그라운드 생성 직후 **딱 한 번만** 사용자에게 노출된다.

```
생성 완료 → 개인키 화면에 표시 (download/copy 가능)
           → 이후 페이지 이탈 시 서버에서는 다시 볼 수 없음
```

> 개인키를 저장하지 않으면? sessionStorage에 탭 세션 동안 보관해두어 새로고침 후에도 복원된다.  
> 탭을 닫으면 자동 삭제된다. localStorage보다 안전한 방식이다.

RSA 4096이 약간 무겁긴 하지만 플레이그라운드 생성이 어차피 15~30초 걸리는 작업이라 체감상 차이가 없었다.

---

## **재미있었던 포인트 4: 멀티 클러스터 지원**

"하나의 클러스터면 충분하지 않나?"라고 생각할 수 있다.

그런데 현실에서는 개발 클러스터, 실습 클러스터, 테스트용 클러스터 등이 분리되어 있는 경우가 많다.

그래서 **kubeconfig를 Secret에 등록하면 여러 클러스터에 플레이그라운드를 배포할 수 있는** 레지스트리 구조를 만들었다.

```
관리자 → kubeconfig YAML 붙여넣기 → K8s Secret 저장
사용자 → 클러스터 선택 → 해당 클러스터에 플레이그라운드 배포
```

클러스터마다 독립적인 `K8sManager` 인스턴스를 생성한다. 인메모리 캐싱을 적용하여 매 요청마다 kubeconfig를 재파싱하지 않도록 했다.

여기서 한 가지 주의점이 있었다. kubeconfig의 `server` 필드에 `https://kubernetes.default.svc`가 들어있으면 외부 클러스터에서는 통하지 않는다. 등록된 kubeconfig에서 실제 API 서버 주소를 추출해서 각 클러스터 연결에 사용해야 한다.

---

## **RBAC 커스터마이징**

관리자는 플레이그라운드 생성 시 사용자의 Kubernetes 접근 범위를 직접 설정할 수 있다.

| namespaces | verbs | 생성되는 리소스 | 실습 범위 |
|---|---|---|---|
| `["sandbox"]` | `["get","list","watch"]` | Role + RoleBinding | sandbox 읽기 전용 |
| `["ns1","ns2"]` | `["*"]` | Role × 2 + RoleBinding × 2 | 지정 네임스페이스 전체 |
| `["*"]` | `["get","list"]` | ClusterRole + ClusterRoleBinding | 클러스터 전체 읽기 |

기본 사용자는 자신의 `sandbox-{id}` 네임스페이스에 ClusterRole `admin`이 바인딩된다.

> 다른 사용자의 sandbox에 접근할 수 없다.  
> K8s RBAC이 네임스페이스 단위로 격리를 보장하기 때문이다.

RBAC은 파드 재시작 없이 실시간으로 변경할 수 있다. K8s RBAC이 API 요청 시점에 평가되기 때문이다.

---

## **배포: Helm Chart**

배포는 Helm으로 통일했다.

관리자 비밀번호 해시를 배포 시점에 동적으로 생성하는 스크립트를 만들었다.

```bash
ADMIN_PASS=secure_password ./helm-deploy.sh
```

내부적으로는 `python3 -c "bcrypt.hashpw(...)"` 를 실행해서 bcrypt 해시를 즉석에서 생성한 뒤 `--set-string`으로 Helm에 전달한다.

덕분에 values.yaml에 평문 비밀번호가 남지 않는다.

```
helm upgrade --install playground-platform ./chart/...
  --set-string adminPasswordHash="$ADMIN_HASH"
```

---

## **실제 사용 흐름**

```
1. 관리자: 클러스터 kubeconfig 등록
2. 사용자: 브라우저로 접속
3. 클러스터 선택 → Start Playground 클릭
4. 15~30초 후 브라우저 터미널 자동 연결
5. kubectl get pods -n sandbox-{id} 실행 가능
6. 원하면 SSH로 로컬 터미널에서도 접속
7. 24시간 후 자동 삭제 (또는 직접 Terminate)
```

---

## **아쉬운 점과 개선 여지**

만들고 보니 몇 가지 아쉬운 점도 보였다.

**보안 측면**

`/clusters` 엔드포인트가 인증 없이 클러스터 목록을 반환한다. 클러스터 이름과 ID만 반환하도록 제한했지만, 인증을 추가하는 것이 더 깔끔하다.

관리자 대시보드에서 자격증명을 sessionStorage에 base64로 저장하는 방식은 XSS에 취약하다. 서버 사이드 세션으로 전환하는 것이 이상적이다.

**운영 측면**

API 서버가 단일 레플리카라면 재시작 시 진행 중인 WebSocket이 끊긴다. 세션 복원이 되긴 하지만 개선 여지가 있다.

ClusterRole `admin` 권한이 sandbox 네임스페이스에 바인딩되어 있어 PVC 같은 클러스터 수준 리소스에는 접근할 수 없다. 이는 의도된 설계이지만 실습 범위를 넓히고 싶다면 조정이 필요하다.

---

## **마무리**

생각보다 재미있었던 프로젝트였다.

처음에는 간단할 줄 알았는데 WebSocket과 kubernetes.stream 연동, K8s Native 세션 관리, 세션별 SSH 키 생성, 멀티 클러스터 레지스트리 등 생각보다 다양한 기술을 엮어야 했다.

특히 *"있는 것을 최대한 활용하자"*는 원칙 — DB 대신 ConfigMap, 암호화 저장소 대신 Secret, 스케줄러 대신 asyncio 루프 — 이 K8s Native 설계의 핵심이라는 걸 다시 한번 느꼈다.

Kubernetes를 운영하는 환경이라면 이미 가지고 있는 인프라를 저장소이자 플랫폼으로 최대한 활용하는 것이 운영 복잡도를 낮추는 좋은 방법이 아닐까 생각한다.

---

## **중요**

잘못된 정보나, 문의등은 댓글로 메일과 함께 적어주시면 감사하겠습니다.
