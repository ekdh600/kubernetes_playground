# Kubernetes Playground Platform — 워크플로우 문서

> **버전**: v17 (2026-02-26 기준)  
> **목적**: 시스템의 주요 기능별 처리 흐름을 단계적으로 기술한다.

---

## 목차

1. [플랫폼 배포 워크플로우](#1-플랫폼-배포-워크플로우)
2. [클러스터 등록 워크플로우](#2-클러스터-등록-워크플로우)
3. [플레이그라운드 생성 워크플로우 (일반 사용자)](#3-플레이그라운드-생성-워크플로우-일반-사용자)
4. [브라우저 터미널 연결 워크플로우](#4-브라우저-터미널-연결-워크플로우)
5. [SSH 직접 접속 워크플로우](#5-ssh-직접-접속-워크플로우)
6. [플레이그라운드 삭제 워크플로우](#6-플레이그라운드-삭제-워크플로우)
7. [관리자 커스텀 플레이그라운드 생성 워크플로우](#7-관리자-커스텀-플레이그라운드-생성-워크플로우)
8. [관리자 RBAC 업데이트 워크플로우](#8-관리자-rbac-업데이트-워크플로우)
9. [자동 만료 정리 워크플로우](#9-자동-만료-정리-워크플로우)
10. [페이지 새로고침 후 세션 복원 워크플로우](#10-페이지-새로고침-후-세션-복원-워크플로우)
11. [오류 처리 및 롤백 워크플로우](#11-오류-처리-및-롤백-워크플로우)

---

## 1. 플랫폼 배포 워크플로우

### 사전 요구사항
- `kubectl`이 대상 클러스터를 가리키도록 설정
- `helm` v3 설치
- `python3` + `bcrypt` 패키지 설치
- Docker 이미지 빌드 완료 (또는 기존 이미지 사용)

### 단계별 흐름

```
[운영자]
    │
    │ ADMIN_PASS=secure_password ./helm-deploy.sh
    ▼
[helm-deploy.sh]
    │
    ├── 1. 사전 점검
    │     ├── helm 바이너리 존재 확인
    │     ├── python3 바이너리 존재 확인
    │     └── ADMIN_PASS 환경변수 필수 확인 (없으면 즉시 종료)
    │
    ├── 2. bcrypt 해시 동적 생성
    │     └── python3 -c "bcrypt.hashpw(ADMIN_PASS, gensalt())"
    │             → ADMIN_HASH = "$2b$12$..."
    │
    └── 3. Helm Upgrade/Install
          helm upgrade --install playground-platform ./chart/...
            --namespace playground-system
            --create-namespace
            --set adminUser=admin
            --set-string adminPasswordHash=$ADMIN_HASH
          │
          ▼
        [Helm이 생성하는 K8s 리소스]
          ├── Namespace: playground-system (--create-namespace)
          ├── ServiceAccount: playground-platform
          ├── ClusterRole: playground-platform-role
          ├── ClusterRoleBinding: playground-platform-binding
          ├── Secret: playground-platform-secrets
          │     (ADMIN_USER, ADMIN_PASSWORD_HASH, NAMESPACE)
          ├── Deployment: playground-platform-api (FastAPI)
          ├── Deployment: playground-platform-ui (Nginx)
          ├── Service: playground-platform (NodePort :30800)
          ├── Service: playground-platform-api (ClusterIP :8000)
          └── Ingress: (ingress.enabled=true 시만 생성)

[API 서버 파드 시작]
    │
    ├── kube_config.yaml 삭제 (개발 파일 제거)
    ├── uvicorn main:app --host 0.0.0.0 --port 8000 실행
    └── lifespan: 백그라운드 cleanup_loop asyncio Task 등록

[배포 완료]
    사용자: http://{노드IP}:30800
    관리자: http://{노드IP}:30800/admin
```

---

## 2. 클러스터 등록 워크플로우

```
[관리자 브라우저 - admin.html Clusters 탭]
    │
    │ 클러스터 이름 입력 + kubeconfig YAML 붙여넣기
    │ Register Cluster 버튼 클릭
    ▼
[admin.html registerCluster()]
    │
    │ POST /admin/clusters
    │   Authorization: Basic base64(admin:pass)
    │   body: { name: "prod-cluster", kubeconfig: "apiVersion: v1..." }
    ▼
[main.py admin_register_cluster()]
    │
    ├── verify_admin() → bcrypt 검증
    └── register_cluster(name, kubeconfig_yaml)
        │
        ▼
    [cluster_registry.py register_cluster()]
        │
        ├── UUID 생성 (cluster_id = 앞 8자리)
        ├── kubeconfig_yaml base64 인코딩
        └── K8s Secret 생성
              playground-cluster-{id} in playground-system
              data: { id, name, kubeconfig, created_at }
        │
        ▼
    [응답 반환]
        │
        ├── 성공: { id, name, message: "Cluster registered" }
        └── 실패: 400 Bad Request (kubeconfig 파싱 오류 등)

[admin.html]
    └── 클러스터 목록 테이블 갱신 + Create Playground 탭 드롭다운 업데이트
```

---

## 3. 플레이그라운드 생성 워크플로우 (일반 사용자)

```
[사용자 브라우저 - index.html]
    │
    │ 1. 페이지 접속 → init() 실행
    ▼
[index.html init()]
    │
    ├── GET /playground/me (세션 쿠키 전송)
    │     │
    │     ├── 기존 playground 있음 → connectTerminal() 호출 (섹션 4로)
    │     └── 없음 → loadClusters() 실행
    │                 GET /clusters → 클러스터 드롭다운 표시
    │
    │ 2. 클러스터 선택 + "Start Playground" 클릭
    ▼
[index.html createPlayground()]
    │
    │ POST /playground
    │   Cookie: session_id=uuid4 (없으면 새로 발급됨)
    │   body: { cluster_id: "f2e8a1b3" }
    ▼
[main.py create_playground()]
    │
    ├── 세션 확인/발급: get_or_create_session_id()
    │     └── 신규면 Response 쿠키에 session_id 설정
    │
    ├── 관리자 우회 확인 (Authorization 헤더 파싱)
    │
    ├── 중복 체크: get_active_playground(session_id)
    │     └── 이미 있고 일반 사용자 → 409 (playground_id 반환)
    │
    ├── 클러스터 확인: get_cluster(cluster_id)
    │
    └── K8s 리소스 생성 순서:
        │
        ├── Step 1. setup_namespace(NAMESPACE)
        │           study 네임스페이스 + playground-sa 생성 (없으면)
        │
        ├── Step 2. create_secret(NAMESPACE, id, public_key, private_key)
        │           ssh-key-{id} Secret 생성
        │
        ├── Step 3. create_sandbox_namespace(sandbox-{id})
        │           sandbox-{id} 네임스페이스 생성
        │
        ├── Step 4. setup_sandbox_rbac(sandbox-{id})
        │           admin-sa ServiceAccount 생성
        │           ClusterRole "admin" RoleBinding 생성
        │
        ├── Step 5. get_service_account_token(sandbox-{id}, admin-sa)
        │           admin-sa-token Secret 생성
        │           K8s 컨트롤러가 토큰 채울 때까지 최대 10초 폴링
        │           JWT 토큰 반환
        │
        ├── Step 6. create_kubeconfig_secret(study, id, sandbox-{id}, token)
        │           kubeconfig-{id} Secret 생성
        │           server: {클러스터 API URL}
        │           certificate-authority: /var/run/secrets/.../ca.crt
        │           token: {JWT}
        │           namespace: sandbox-{id}
        │
        ├── Step 7. create_deployment(study, id, ssh-key-{id}, kubeconfig-{id})
        │           ubuntu-sshd-{id} Deployment 생성
        │           컨테이너: playground-runner:v4
        │           볼륨: /keys ← ssh-key-{id}, /kubeconfig ← kubeconfig-{id}
        │
        ├── Step 8. create_service(study, id)
        │           ubuntu-sshd-svc-{id} NodePort Service 생성
        │           port: 2222, nodePort: 자동 할당
        │
        ├── Step 9. get_service_node_port() + get_pod_node_ip()
        │           서비스 NodePort 번호 조회
        │           파드 스케줄 노드 IP 조회
        │
        └── Step 10. bind_playground(session_id, playground_id, cluster_id)
                    세션 ConfigMap에 바인딩 정보 저장

[응답 반환]
    │
    └── 200 OK:
        {
          id: "ab3c7d91",
          user: "ubuntu",
          host: "10.0.0.10",
          port: 31234,
          private_key: "-----BEGIN OPENSSH PRIVATE KEY-----...",
          cluster_id: "f2e8a1b3",
          message: "Playground created successfully."
        }

[index.html]
    ├── private_key → sessionStorage 저장 + 화면에 표시
    ├── SSH 접속 정보 드로어 업데이트
    └── 1.2초 후 connectTerminal() 호출 (섹션 4로)
```

---

## 4. 브라우저 터미널 연결 워크플로우

```
[index.html connectTerminal(playgroundId, clusterId)]
    │
    ├── 프로토콜 결정: HTTPS → wss://, HTTP → ws://
    ├── URL 구성: /ws/{id}?cluster_id={cid}&token={private_key}
    └── WebSocket 연결 시도
    ▼
[Nginx] Upgrade: websocket 헤더 전달
    ▼
[main.py websocket_endpoint()]
    │
    ├── 1. await websocket.accept()
    │
    ├── 2. 클러스터 매니저 확인
    │     get_cached_manager(cluster_id)
    │
    ├── 3. 소유권 검증
    │     ├── 방법 A: 세션 쿠키로 playground_id 대조
    │     └── 방법 B: token(private_key) == K8s Secret의 저장된 키 대조
    │     → 미인증 시 403 전송 후 close(1008)
    │
    ├── 4. 파드 조회
    │     list_namespaced_pod(label_selector="instance={id}")
    │     → 파드 없음 시 오류 메시지 후 close(1000)
    │
    ├── 5. 파드 Ready 대기 (최대 120초)
    │     loop(120):
    │       read_namespaced_pod()
    │       phase == "Running" AND all containers ready → break
    │       아니면 "\r🔄 Playground is initializing... (Ns)" 전송
    │       await asyncio.sleep(1)
    │     120초 초과 → "Initialization timed out" 전송 후 close
    │
    ├── 6. Pod exec 스트림 오픈
    │     kubernetes.stream.connect_get_namespaced_pod_exec
    │     command=["su", "-", "ubuntu"]
    │     stdin=True, stdout=True, stderr=True, tty=True
    │
    ├── 7. "\r\n*** Connected to Playground ***" 전송
    │
    └── 8. 양방향 스트리밍 (asyncio.gather)
          │
          ├── k8s_to_ws() [별도 스레드 - asyncio.to_thread]
          │     loop while resp.is_open():
          │       resp.update(timeout=0.1)
          │       peek_stdout → read_stdout → run_coroutine_threadsafe(ws.send_text)
          │       peek_stderr → read_stderr → run_coroutine_threadsafe(ws.send_text)
          │
          └── ws_to_k8s() [이벤트 루프 - asyncio 코루틴]
                loop:
                  await asyncio.wait_for(ws.receive_text(), timeout=3600)
                  resp.write_stdin(data)
                  TimeoutError → 비활성 타임아웃 (1시간)
                  WebSocketDisconnect → 브라우저 탭 닫힘
                finally: resp.close()

[xterm.js]
    ├── ws.onmessage → term.write(d)
    └── term.onData → ws.send(d)
```

---

## 5. SSH 직접 접속 워크플로우

```
[사용자]
    │
    │ 1. index.html SSH 드로어에서 명령어 복사
    │    "ssh ubuntu@10.0.0.10 -p 31234 -i playground_key.pem"
    │
    │ 2. "Download Private Key (.pem)" 클릭
    ▼
[index.html downloadKey()]
    │
    ├── 키 소스 확인:
    │   1순위: privateKeyData (메모리)
    │   2순위: sessionStorage['pg_key_{id}']
    │   3순위: pkeyText 텍스트박스 값
    │
    └── Blob URL로 playground_key.pem 다운로드

[사용자 로컬 터미널]
    │
    │ ssh ubuntu@10.0.0.10 -p 31234 -i playground_key.pem
    ▼
    ├── chmod 600 playground_key.pem (필요 시)
    ▼
[노드 IP:31234 → NodePort]
    ▼
[playground-runner 파드 sshd]
    │
    ├── RSA 공개키 검증 (authorized_keys와 대조)
    └── 인증 성공 → ubuntu 사용자 bash 쉘
                    KUBECONFIG=/home/ubuntu/.kube/config
                    kubectl → sandbox-{id} 네임스페이스
```

---

## 6. 플레이그라운드 삭제 워크플로우

### 6.1 사용자 직접 삭제

```
[index.html confirmDelete()]
    │
    │ DELETE /playground/{id}
    │   Cookie: session_id=uuid4
    ▼
[main.py delete_playground()]
    │
    ├── 세션에서 활성 playground 조회
    ├── playground_id 일치 확인 (불일치 시 403)
    │
    └── K8s 리소스 삭제:
        ├── mgr.delete_playground(study, id)
        │     ├── Service 삭제 (label: instance={id})
        │     ├── Deployment 삭제 (label: instance={id})
        │     ├── ClusterRole/RoleBinding 삭제 (커스텀 RBAC)
        │     ├── ssh-key-{id} Secret 삭제
        │     └── kubeconfig-{id} Secret 삭제
        │
        ├── core_v1.delete_namespace("sandbox-{id}")
        │     └── 내부 모든 리소스(SA, Role, Token) 함께 삭제
        │
        └── clear_all_sessions_for_playground(id)
              └── 모든 세션 ConfigMap의 바인딩 해제

[응답]
    └── 200 OK: { message: "Playground {id} deleted." }

[index.html]
    ├── sessionStorage에서 pg_key_{id} 삭제
    └── location.reload() (Landing 화면으로 복귀)
```

### 6.2 관리자 강제 삭제

```
[admin.html delPlayground(id, clusterId)]
    │
    │ DELETE /admin/playgrounds/{id}?cluster_id={cid}
    │   Authorization: Basic ...
    ▼
[main.py admin_delete_playground()]
    │
    ├── verify_admin() 인증 확인
    └── (사용자 소유권 확인 없음)
        └── 위 6.1과 동일한 삭제 순서
```

---

## 7. 관리자 커스텀 플레이그라운드 생성 워크플로우

```
[admin.html Create Playground 탭]
    │
    ├── 클러스터 선택
    ├── 네임스페이스 선택 (체크박스)
    │     ├── All (*): 클러스터 전체 권한 (ClusterRole 생성)
    │     ├── sandbox: 전용 sandbox만
    │     └── 특정 네임스페이스: 해당 네임스페이스에 Role 생성
    │
    ├── 동사 입력 (예: "get, list, watch, create")
    └── Create 버튼 클릭
    │
    │ POST /admin/playgrounds
    │   Authorization: Basic ...
    │   body: { cluster_id, namespaces: ["sandbox", "ns1"], verbs: ["get","list"] }
    ▼
[main.py admin_create_playground()]
    │
    └── K8s 리소스 생성 (일반 생성과 동일하지만 4단계가 다름):
        │
        ├── Step 1~3: setup_namespace, create_secret, create_sandbox_namespace
        │
        ├── Step 4. setup_custom_rbac(sandbox_ns, namespaces, verbs, id)
        │
        │   [namespaces=["*"]]
        │     ├── ClusterRole playground-custom-{id} 생성
        │     │     (레이블: instance={id}, cluster={cid})
        │     └── ClusterRoleBinding playground-custom-{id} 생성
        │
        │   [namespaces=["sandbox", "ns1"]]
        │     ├── "sandbox" → sandbox-{id} 네임스페이스로 치환
        │     └── 각 네임스페이스마다:
        │           Role playground-custom-{id} 생성
        │           RoleBinding playground-custom-{id} 생성
        │
        ├── Step 5~9: get_service_account_token, create_kubeconfig_secret,
        │             create_deployment, create_service,
        │             get_service_node_port + get_pod_node_ip
        │
        └── Step 10. Service 어노테이션에 RBAC 현황 저장
              playground.namespaces: "sandbox,ns1"
              playground.verbs: "get,list,watch"

[응답]
    └── 200 OK: PlaygroundResponse (id, host, port, private_key 포함)

[admin.html]
    ├── 성공 메시지 표시
    └── Playgrounds 탭으로 자동 이동
```

---

## 8. 관리자 RBAC 업데이트 워크플로우

```
[admin.html - 플레이그라운드 테이블]
    │
    │ Edit 버튼 클릭
    ▼
[openEditRbac(id, clusterId, currentNs, currentVerbs)]
    │
    ├── 현재 RBAC 설정으로 모달 초기화
    ├── fetchNamespaces(clusterId): 클러스터 네임스페이스 목록 로드
    └── 모달 표시
    │
    │ 새 네임스페이스/동사 설정 후 Save Changes 클릭
    ▼
[submitEditRbac()]
    │
    │ PUT /admin/playgrounds/{id}/rbac
    │   body: { cluster_id, namespaces, verbs }
    ▼
[main.py admin_update_playground_rbac()]
    │
    └── mgr.update_custom_rbac(study, id, sandbox-{id}, namespaces, verbs)
        │
        ├── 1. delete_custom_rbac(id)
        │     label_selector: "instance={id},cluster={cid}"
        │     ClusterRole, ClusterRoleBinding, Role, RoleBinding 전부 삭제
        │
        ├── 2. setup_custom_rbac(sandbox-{id}, namespaces, verbs, id)
        │     새 RBAC 재생성
        │
        └── 3. patch_namespaced_service(ubuntu-sshd-svc-{id})
              어노테이션 업데이트 (목록 표시용)

[파드 영향]
    파드 재시작 없음. K8s RBAC은 실시간 반영되므로
    kubectl 명령 실행 시 즉시 새 권한이 적용된다.

[admin.html]
    ├── "RBAC Updated Successfully" 메시지
    ├── 0.7초 후 모달 닫기
    └── 플레이그라운드 목록 새로고침
```

---

## 9. 자동 만료 정리 워크플로우

```
[서버 시작 시]
    lifespan() → asyncio.create_task(cleanup_loop())
    │
    ▼
[cleanup_loop() - 60초마다 반복]
    │
    ├── for cluster in list_clusters():
    │     mgr = get_cached_manager(cluster.id)
    │     mgr.cleanup_expired_playgrounds(study, max_age=86400)
    │     │
    │     └── list_namespaced_service(label="app=playground")
    │           for svc in services:
    │             age = now - svc.creation_timestamp
    │             if age > 86400:  # 24시간
    │               instance_id = svc.labels["instance"]
    │               delete_playground(study, instance_id)
    │               delete_namespace("sandbox-{id}")
    │
    └── cleanup_expired_sessions()
          list_namespaced_config_map(label="type=session")
          for cm in configmaps:
            if now > cm.data["expires_at"]:
              delete_namespaced_config_map(cm.name)

[삭제 대상]
    ├── 생성 후 24시간 이상 경과한 플레이그라운드
    │   (SSH 연결 중이어도 파드는 삭제됨)
    └── 만료된 세션 ConfigMap
        (브라우저 탭이 열려 있어도 삭제됨)
```

---

## 10. 페이지 새로고침 후 세션 복원 워크플로우

```
[사용자가 브라우저 새로고침]
    │
    ▼
[index.html init()]
    │
    ├── 1. GET /playground/me (session_id 쿠키 자동 포함)
    │       │
    │       └── [main.py my_playground()]
    │               get_active_playground(session_id)
    │               → ConfigMap에서 playground_id, cluster_id 조회
    │               → 만료 확인 (expires_at > now)
    │               → SSH 정보 enrichment (best-effort)
    │               → 응답: { playground_id, cluster_id, host, port, ... }
    │
    ├── 2. data.playground_id 존재 확인
    │
    ├── 3. sessionStorage에서 개인키 복원
    │       const storedKey = sessionStorage.getItem('pg_key_{id}')
    │       if storedKey → privateKeyData = storedKey
    │
    ├── 4. SSH 정보 업데이트 (host/port 있으면)
    │
    └── 5. 0.6초 후 connectTerminal() 호출
              WebSocket 재연결
              파드 Ready 대기 루프 (이미 실행 중이면 즉시 연결)

[복원 불가 케이스]
    ├── sessionStorage 없음 (새 탭에서 열기, 프라이빗 모드)
    │   → SSH 접속 정보는 표시되지만 다운로드 버튼 숨김
    └── 세션 만료 (24시간 초과)
        → 빈 응답 → 클러스터 선택 화면 표시
```

---

## 11. 오류 처리 및 롤백 워크플로우

### 11.1 플레이그라운드 생성 중 오류

```
[create_playground() 실행 중 예외 발생]
    │
    └── except Exception as e:
          │
          ├── mgr.delete_playground(study, playground_id)
          │     Service, Deployment, Secret 삭제 시도
          │     (이미 삭제되었거나 생성 전이라면 오류 무시)
          │
          ├── core_v1.delete_namespace("sandbox-{id}")
          │     sandbox 네임스페이스 삭제 시도
          │     (오류 발생해도 무시 - except: pass)
          │
          └── raise HTTPException(500, detail=str(e))
                사용자에게 500 오류 반환

[주의사항]
    세션 ConfigMap에는 bind_playground()가 호출되기 전 오류가 발생하므로
    실패한 플레이그라운드는 세션에 바인딩되지 않는다.
    사용자는 다시 "Start Playground" 버튼을 클릭할 수 있다.
```

### 11.2 클러스터 접근 불가 오류

```
[get_cached_manager(cluster_id) 호출]
    │
    ├── Secret에서 kubeconfig 읽기
    ├── K8sManager 생성 (연결 타임아웃: 10초)
    │
    └── 클러스터 접근 불가 시 (네트워크 오류, 인증 만료 등):
          API 호출 시점에 예외 발생
          → HTTPException(500) 반환
          → 캐시에는 저장되지 않음
              (다음 요청 시 재시도 가능)
```

### 11.3 WebSocket 연결 오류

```
[websocket_endpoint() 오류 케이스]

오류 유형                          처리 방법
─────────────────────────────────────────────────────
클러스터 매니저 없음              오류 메시지 전송 + close(1011)
소유권 검증 실패                  Forbidden 메시지 전송 + close(1008)
파드 없음                         Pod not found 메시지 + close(1000)
파드 Ready 타임아웃 (120초)       Initialization timed out + close(1000)
K8s exec 스트림 오류              print(error) + websocket.close()
WebSocket 비활성 (1시간)          TimeoutError → resp.close()
브라우저 탭 닫힘                   WebSocketDisconnect → resp.close()
```

### 11.4 세션 오류 처리

```
[session_manager.py 오류 케이스]

오류 유형                          처리 방법
─────────────────────────────────────────────────────
ConfigMap 없음 (404)             None 반환 (새 세션으로 처리)
K8s API 일시 장애                None 반환 (기능 저하 허용)
ConfigMap 생성 실패              로그 출력 후 세션 ID만 반환
bind_playground 실패 (404)       ConfigMap 새로 생성 후 재시도
```

---

## 부록: 전체 흐름 요약

```
일반 사용자 시나리오:
  브라우저 접속 → 클러스터 선택 → Start Playground
  → K8s 리소스 자동 생성 (15~30초)
  → 브라우저 터미널 연결
  → kubectl 실습 (sandbox 네임스페이스)
  → 선택: SSH 직접 접속 (로컬 터미널)
  → Terminate 버튼 or 24시간 후 자동 삭제

관리자 시나리오:
  /admin 접속 → HTTP Basic Auth 로그인
  → Clusters 탭: 클러스터 등록 (kubeconfig 붙여넣기)
  → Create Playground 탭: RBAC 커스터마이즈 플레이그라운드 생성
  → Playgrounds 탭: 전체 현황 조회, RBAC 편집, 터미널 접속, 삭제
```
