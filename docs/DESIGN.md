# Kubernetes Playground Platform — 시스템 설계서

> **버전**: v17 (2026-02-26 기준)  
> **작성 목적**: 시스템 아키텍처, 컴포넌트 설계, 데이터 모델, 보안 모델, 배포 전략을 기술한다.

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [시스템 아키텍처](#2-시스템-아키텍처)
3. [컴포넌트 설계](#3-컴포넌트-설계)
4. [데이터 모델](#4-데이터-모델)
5. [네트워크 설계](#5-네트워크-설계)
6. [보안 모델](#6-보안-모델)
7. [멀티 클러스터 아키텍처](#7-멀티-클러스터-아키텍처)
8. [플레이그라운드 격리 설계](#8-플레이그라운드-격리-설계)
9. [세션 관리 설계](#9-세션-관리-설계)
10. [배포 아키텍처](#10-배포-아키텍처)
11. [기술 스택 요약](#11-기술-스택-요약)

---

## 1. 프로젝트 개요

### 목적
Kubernetes Playground Platform은 Kubernetes 학습자에게 **온디맨드 격리 실습 환경**을 제공하는 플랫폼이다. 사용자는 별도의 설치 없이 브라우저만으로 완전한 Kubernetes 실습 환경(kubectl 접근 가능한 터미널)을 사용할 수 있다.

### 핵심 특징
- **완전 자동화**: 클릭 한 번으로 K8s 네임스페이스, RBAC, SSH 서버 파드, 서비스가 자동 생성
- **브라우저 터미널**: xterm.js + WebSocket으로 별도 SSH 클라이언트 없이 터미널 접근
- **외부 SSH 접속**: NodePort 서비스로 로컬 터미널(PuTTY, ssh 명령어)에서도 접속 가능
- **멀티 클러스터**: 여러 Kubernetes 클러스터를 등록하여 분산 관리
- **RBAC 커스터마이징**: 관리자가 사용자별 Kubernetes 접근 범위를 동적으로 제어
- **세션 격리**: UUID 기반 쿠키로 1인 1플레이그라운드 정책 적용

---

## 2. 시스템 아키텍처

### 전체 구성도

```
┌───────────────────────────────────────────────────────────────────────┐
│                         사용자 브라우저                                  │
│  ┌──────────────────────┐    ┌───────────────────────────────────────┐ │
│  │    index.html         │    │         admin.html                    │ │
│  │  (xterm.js 터미널 UI) │    │    (관리자 대시보드)                    │ │
│  └──────────┬───────────┘    └──────────────────┬────────────────────┘ │
└─────────────┼─────────────────────────────────────┼────────────────────┘
              │ HTTP/HTTPS, WebSocket               │ HTTP Basic Auth
              ▼                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                   playground-system Namespace                            │
│                                                                          │
│  ┌──────────────────────────┐    ┌──────────────────────────────────┐   │
│  │  playground-platform-ui   │    │    playground-platform-api       │   │
│  │  (Nginx, NodePort:30800) │    │    (FastAPI, ClusterIP:8000)     │   │
│  │                          │    │                                  │   │
│  │  - 정적 파일 서빙         │    │  main.py (게이트웨이)             │   │
│  │  - API 역방향 프록시      │───►│  k8s_manager.py (K8s 관리)      │   │
│  │  - WS 업그레이드 처리     │    │  session_manager.py (세션)       │   │
│  └──────────────────────────┘    │  cluster_registry.py (클러스터)  │   │
│                                  │  auth.py (인증)                  │   │
│                                  │  key_manager.py (SSH 키)         │   │
│                                  └──────────────┬───────────────────┘   │
│                                                 │                       │
│  ┌──────────────────────────────────────────────┼──────────────────┐   │
│  │  K8s Native Storage                          │ K8s API          │   │
│  │                                              │                  │   │
│  │  playground-session-{uuid}  ConfigMap ◄──────┤                  │   │
│  │  playground-cluster-{id}    Secret    ◄──────┤                  │   │
│  └──────────────────────────────────────────────┴──────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
              │
              │ K8s API (ClusterRole 권한)
              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                   study Namespace (플레이그라운드 호스트)                  │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  Playground Instance (per user)                                  │   │
│  │                                                                  │   │
│  │  ubuntu-sshd-{id} Deployment ──► Pod (playground-runner:v4)     │   │
│  │  ubuntu-sshd-svc-{id} Service (NodePort)                        │   │
│  │  ssh-key-{id} Secret (authorized_keys + private_key)            │   │
│  │  kubeconfig-{id} Secret (sandbox kubeconfig + SA token)         │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
              │
              │ kubectl (kubeconfig Secret)
              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                   sandbox-{id} Namespace (사용자 격리 공간)               │
│                                                                          │
│  admin-sa ServiceAccount  ←  ClusterRole "admin" RoleBinding           │
│  admin-sa-token Secret (SA 토큰)                                         │
│                                                                          │
│  [사용자가 kubectl로 이 네임스페이스 내에서 자유롭게 실습]                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 레이어 분리

| 레이어 | 네임스페이스 | 역할 |
|--------|------------|------|
| **플랫폼** | `playground-system` | API 서버, UI 서버, 세션 ConfigMap, 클러스터 Secret |
| **호스트** | `study` | 플레이그라운드 Deployment, Service, SSH 키 Secret, kubeconfig Secret |
| **샌드박스** | `sandbox-{id}` | 사용자 격리 실습 공간, admin-sa, RBAC |

---

## 3. 컴포넌트 설계

### 3.1 main.py — API 게이트웨이

**역할**: FastAPI 기반 HTTP REST API + WebSocket 게이트웨이

**주요 엔드포인트**:

| 메서드 | 경로 | 인증 | 설명 |
|--------|------|------|------|
| POST | `/playground` | 세션 쿠키 | 플레이그라운드 생성 |
| DELETE | `/playground/{id}` | 세션 쿠키 | 플레이그라운드 삭제 |
| GET | `/playground/me` | 세션 쿠키 | 현재 세션 조회 |
| GET | `/clusters` | 없음 | 클러스터 목록 (id/name만) |
| WS | `/ws/{id}` | 쿠키 or token | 터미널 WebSocket |
| GET/POST/DELETE | `/admin/*` | HTTP Basic Auth | 관리자 전용 |

**백그라운드 루프**:
- 60초마다 실행
- 24시간 초과 플레이그라운드 자동 삭제
- 만료된 세션 ConfigMap 정리

### 3.2 k8s_manager.py — Kubernetes 리소스 관리자

**역할**: K8s API 서버와 통신하여 플레이그라운드 리소스 생명주기를 관리

**주요 메서드**:

| 메서드 | 설명 |
|--------|------|
| `setup_namespace()` | study 네임스페이스 + playground-sa 생성 |
| `create_sandbox_namespace()` | sandbox-{id} 네임스페이스 생성 |
| `setup_sandbox_rbac()` | admin-sa + ClusterRole "admin" RoleBinding |
| `setup_custom_rbac()` | 커스텀 네임스페이스/동사 조합 RBAC |
| `get_service_account_token()` | Legacy SA Token Secret 방식 JWT 발급 |
| `create_kubeconfig_secret()` | 파드 내 kubectl용 kubeconfig Secret |
| `create_secret()` | SSH 키 Secret (authorized_keys + private_key) |
| `create_deployment()` | playground-runner 파드 배포 |
| `create_service()` | NodePort SSH 서비스 생성 |
| `cleanup_expired_playgrounds()` | 24시간 초과 플레이그라운드 정리 |
| `delete_playground()` | 플레이그라운드 전체 삭제 |

**설계 원칙**:
- 인스턴스별 독립 `ApiClient` (멀티 클러스터 지원, 전역 상태 오염 방지)
- kubeconfig 인메모리 dict 방식 (파일 노출 없음)
- 409(이미 존재) 응답 멱등 처리

### 3.3 session_manager.py — 세션 관리

**역할**: 익명 UUID 기반 세션과 플레이그라운드 바인딩 관리

**저장 구조 (Kubernetes ConfigMap)**:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: playground-session-{uuid}
  namespace: playground-system
  labels:
    playground.k8s-playground.io/type: session
data:
  created_at: "2026-01-01T00:00:00"
  expires_at: "2026-01-02T00:00:00"    # 24시간 후
  playground_id: "ab3c7d91"            # 바인딩된 플레이그라운드 ID
  cluster_id: "f2e8a1b3"              # 배포된 클러스터 ID
```

### 3.4 cluster_registry.py — 클러스터 레지스트리

**역할**: 외부 Kubernetes 클러스터 kubeconfig 등록/관리

**저장 구조 (Kubernetes Secret)**:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: playground-cluster-{id}
  namespace: playground-system
  labels:
    playground.k8s-playground.io/type: cluster
type: Opaque
data:
  id: base64(cluster_id)
  name: base64(cluster_name)
  kubeconfig: base64(kubeconfig_yaml)
  created_at: base64(timestamp)
```

**인메모리 캐시**:
- K8sManager 인스턴스를 `_manager_cache` 딕셔너리에 캐싱
- 클러스터 삭제 시 `invalidate_cache()` 호출로 갱신

### 3.5 auth.py — 관리자 인증

**인증 방식**: HTTP Basic Auth + bcrypt

**보안 설계**:
- `secrets.compare_digest()`: timing attack 방지
- `bcrypt.checkpw()`: GPU brute-force 지연
- `ADMIN_PASSWORD_HASH` 미설정 시 서버 기동 거부

### 3.6 key_manager.py — SSH 키 생성

**역할**: 플레이그라운드별 임시 RSA 4096-bit SSH 키 쌍 생성

**설계 이유**:
- 세션마다 새 키 쌍 → 키 유출 시 피해 범위 단일 플레이그라운드로 제한
- NoEncryption (패스프레이즈 없음) → 브라우저 터미널 자동 인증 가능
- OpenSSH 포맷 → 모든 SSH 클라이언트 호환

---

## 4. 데이터 모델

### 4.1 플레이그라운드 인스턴스

플레이그라운드는 K8s 리소스의 집합으로 존재한다. 별도의 데이터베이스 레코드가 없으며, 리소스의 존재 자체가 플레이그라운드의 상태를 나타낸다.

```
study Namespace:
  ubuntu-sshd-{id}         Deployment   (파드 실행)
  ubuntu-sshd-svc-{id}     Service      (NodePort SSH 노출)
  ssh-key-{id}             Secret       (SSH 키)
  kubeconfig-{id}          Secret       (kubectl 설정)

sandbox-{id} Namespace:
  admin-sa                 ServiceAccount
  admin-rb                 RoleBinding (또는 ClusterRoleBinding)
  admin-sa-token           Secret (SA JWT 토큰)
  playground-custom-{id}  ClusterRole/Role (커스텀 RBAC 시)
```

### 4.2 세션 데이터 흐름

```
[브라우저]
  쿠키: session_id=uuid4
  sessionStorage: pg_key_{id}=private_key_pem

[playground-system Namespace]
  ConfigMap: playground-session-{uuid4}
    data.playground_id = "{id}"
    data.cluster_id = "{cluster_id}"
    data.expires_at = "{iso_timestamp}"
```

---

## 5. 네트워크 설계

### 5.1 트래픽 흐름

```
브라우저
  │
  │ HTTP/HTTPS :80
  ▼
NodePort Service (playground-platform, :30800)
  │
  │ TCP :80
  ▼
Nginx UI 컨테이너
  │
  ├── 정적 파일 (/, /admin, /static/) ──► /usr/share/nginx/html/
  │
  └── API 프록시 (그 외 모든 경로) ──► http://{release}-api.{ns}:8000
        │
        ▼
      ClusterIP Service (playground-platform-api, :8000)
        │
        ▼
      FastAPI API 서버
        │
        ├── REST API ──► K8s API 서버
        │
        └── WebSocket ──► K8s Pod exec stream (kubernetes.stream)
```

### 5.2 WebSocket 연결

```
브라우저 xterm.js
  │
  │ ws:// (HTTP) 또는 wss:// (HTTPS)
  │ /ws/{playground_id}?cluster_id={id}&token={key}
  ▼
Nginx (Upgrade: websocket 헤더 전달)
  │
  ▼
FastAPI WebSocket 엔드포인트
  │
  ├── k8s_to_ws (asyncio.to_thread) ──► Pod stdout/stderr 폴링 ──► WebSocket 전송
  └── ws_to_k8s ──────────────────── WebSocket 키 입력 ──► Pod stdin 전달
```

### 5.3 SSH 직접 접속

```
사용자 로컬 터미널 (ssh 명령어)
  │
  │ TCP :{nodePort} (30000~32767 범위, 자동 할당)
  ▼
노드 IP (InternalIP 또는 ExternalIP)
  │
  │ NodePort → TargetPort :2222
  ▼
playground-runner 파드 sshd
  │
  │ authorized_keys 인증 (RSA 공개키 대조)
  ▼
ubuntu 사용자 쉘
  │
  │ KUBECONFIG=/home/ubuntu/.kube/config
  ▼
kubectl → sandbox-{id} namespace
```

---

## 6. 보안 모델

### 6.1 사용자 인증/격리

| 계층 | 방법 | 목적 |
|------|------|------|
| 세션 | UUID 쿠키 (httponly, samesite=strict) | XSS/CSRF 방지, 1인 1환경 |
| SSH | RSA 4096-bit 키 쌍 | 비밀번호 없는 SSH 인증 |
| K8s | RBAC (RoleBinding, sandbox 한정) | 다른 사용자 네임스페이스 접근 차단 |
| WebSocket | 세션 쿠키 or private_key 토큰 대조 | 타인 터미널 하이재킹 방지 |

### 6.2 관리자 인증

| 항목 | 구현 |
|------|------|
| 프로토콜 | HTTP Basic Auth |
| 비밀번호 | bcrypt 해시 (환경변수) |
| 비교 | secrets.compare_digest (timing attack 방지) |
| 저장 | K8s Secret → 환경변수 주입 |

### 6.3 플레이그라운드 격리

```
sandbox-{id} Namespace
  ↑
  admin-sa ServiceAccount
  ↑
  RoleBinding (scope: sandbox-{id}만)
  ↑
  ClusterRole "admin" (K8s 내장)

결과: 사용자는 자신의 sandbox 네임스페이스 내에서만 관리자 수준 권한을 가짐.
     다른 사용자의 sandbox, study, playground-system 등에 접근 불가.
```

### 6.4 커스텀 RBAC 권한 범위

| namespaces 설정 | verbs 설정 | 생성되는 K8s 리소스 | 권한 범위 |
|----------------|-----------|-------------------|---------| 
| `["sandbox"]` | `["get","list","watch"]` | Role + RoleBinding | sandbox 내 읽기 전용 |
| `["ns1","ns2"]` | `["*"]` | Role + RoleBinding × 2 | 지정 네임스페이스 전체 |
| `["*"]` | `["get","list"]` | ClusterRole + ClusterRoleBinding | 클러스터 전체 읽기 전용 |
| `["*"]` | `["*"]` | ClusterRole + ClusterRoleBinding | ⚠️ 클러스터 전체 관리자 |

---

## 7. 멀티 클러스터 아키텍처

### 7.1 클러스터 등록 구조

```
관리자 POST /admin/clusters
  { name: "prod-cluster", kubeconfig: "apiVersion: v1..." }
  │
  ▼
cluster_registry.py
  │
  ├── UUID 생성 (cluster_id)
  ├── kubeconfig YAML base64 인코딩
  └── K8s Secret 저장
      playground-cluster-{id} in playground-system
```

### 7.2 클러스터별 K8sManager

```
get_cached_manager(cluster_id)
  │
  ├── 캐시 Hit → 기존 K8sManager 반환
  │
  └── 캐시 Miss
      │
      ├── Secret에서 kubeconfig 조회
      ├── yaml.safe_load() → dict
      ├── clusters[0].cluster.server 추출 (server_url)
      └── K8sManager(cluster_id, kubeconfig_dict, server_url) 생성
              │
              └── 독립 ApiClient (전용 연결 풀)
```

### 7.3 클러스터 식별 레이블

모든 플레이그라운드 K8s 리소스에 `cluster: {cluster_id}` 레이블이 부착된다.

```yaml
labels:
  app: playground
  instance: ab3c7d91      # 플레이그라운드 ID
  cluster: f2e8a1b3       # 클러스터 ID (멀티클러스터 구분용)
```

이 레이블로 멀티클러스터 환경에서 `list_playgrounds()`, `cleanup_expired_playgrounds()` 시 자신의 클러스터 리소스만 필터링한다.

---

## 8. 플레이그라운드 격리 설계

### 8.1 네임스페이스 구조

```
study                          ← 플레이그라운드 파드가 실행되는 공간
  ubuntu-sshd-ab3c7d91         ← Deployment (사용자 파드)
  ubuntu-sshd-svc-ab3c7d91     ← Service (SSH 외부 노출)
  ssh-key-ab3c7d91             ← Secret (SSH 키)
  kubeconfig-ab3c7d91          ← Secret (kubectl 설정)

sandbox-ab3c7d91               ← 사용자의 kubectl 실습 공간
  admin-sa                     ← ServiceAccount
  admin-rb                     ← RoleBinding (study ns에서 admin-sa에 권한 부여)
  admin-sa-token               ← JWT 토큰 Secret
```

### 8.2 파드 내부 구조

```
playground-runner:v4 파드
  │
  ├── /keys/authorized_keys     ← K8s Secret 마운트 (SSH 공개키)
  ├── /kubeconfig/config        ← K8s Secret 마운트 (kubectl 설정)
  │
  ├── entrypoint.sh 실행
  │   ├── /keys → ~/.ssh/authorized_keys 복사
  │   ├── /kubeconfig/config → ~/.kube/config 복사
  │   └── sshd -D -e 기동
  │
  └── ubuntu 사용자
      ├── kubectl → sandbox-ab3c7d91 네임스페이스 제한 접근
      └── bash 쉘
```

### 8.3 리소스 생명주기

```
생성 (약 15~30초 소요):
  create_secret → create_sandbox_namespace → setup_sandbox_rbac
  → get_service_account_token → create_kubeconfig_secret
  → create_deployment → create_service → bind_playground

활성 상태:
  WebSocket 터미널 연결 가능 (최대 1시간 비활성 타임아웃)
  SSH 직접 접속 가능 (NodePort)
  24시간 후 자동 만료

삭제:
  delete_playground (Service, Deployment, Secret 삭제)
  + delete_namespace (sandbox-{id} 전체 삭제)
  + clear_all_sessions_for_playground (세션 ConfigMap 바인딩 해제)
```

---

## 9. 세션 관리 설계

### 9.1 세션 쿠키 정책

| 속성 | 값 | 이유 |
|------|------|------|
| `httponly` | True | JavaScript 접근 차단 (XSS 방지) |
| `samesite` | strict | 외부 사이트 요청 시 쿠키 미포함 (CSRF 방지) |
| `max_age` | 86400 (24시간) | 플레이그라운드 수명과 일치 |

### 9.2 1인 1플레이그라운드 정책

```
POST /playground 요청
  │
  ├── get_active_playground(session_id)
  │   │
  │   ├── 활성 playground 없음 → 생성 진행
  │   │
  │   └── 활성 playground 있음
  │       │
  │       ├── is_admin == True → 제한 우회, 새 플레이그라운드 생성
  │       └── is_admin == False → 409 반환 (기존 playground_id 포함)
  │
  └── bind_playground(session_id, playground_id, cluster_id)
```

### 9.3 세션 복원 흐름

페이지 새로고침 시:
1. `GET /playground/me` → session_id 쿠키 기반 기존 플레이그라운드 조회
2. SSH 개인키는 `sessionStorage['pg_key_{id}']`에서 복원
3. WebSocket 재연결

---

## 10. 배포 아키텍처

### 10.1 Helm Chart 구조

```
chart/playground-platform/
├── Chart.yaml              # 차트 메타데이터
├── values.yaml             # 기본값 (이미지 태그, 리소스 설정, 관리자 계정)
└── templates/
    ├── serviceaccount.yaml   # playground-platform SA
    ├── clusterrole.yaml      # 플랫폼 ClusterRole
    ├── clusterrolebinding.yaml # SA ↔ ClusterRole 바인딩
    ├── deployment.yaml       # API 서버 + UI 서버 Deployment
    ├── service.yaml          # NodePort(UI) + ClusterIP(API)
    ├── secret.yaml           # 관리자 자격증명 Secret
    └── ingress.yaml          # 선택적 Ingress (ingress.enabled=true 시)
```

### 10.2 이미지 빌드 구조

| 이미지 | Dockerfile | 내용 |
|--------|-----------|------|
| `playground-platform:v17` | `Dockerfile.platform` | FastAPI 앱 + Python 의존성 |
| `playground-platform-ui:v17` | `Dockerfile.ui` | Nginx + 정적 HTML/JS |
| `playground-runner:v4` | 별도 레포 | Ubuntu SSH 서버 (entrypoint.sh 포함) |

### 10.3 배포 명령어

```bash
# Helm 배포 (bcrypt 해시 자동 생성)
ADMIN_PASS="secure_password" ./helm-deploy.sh

# 특정 이미지 태그 지정
ADMIN_PASS="secure_password" ./helm-deploy.sh --set image.tag=v18 --set ui.image.tag=v18

# 네임스페이스 변경
ADMIN_PASS="secure_password" NAMESPACE_PLATFORM=my-ns ./helm-deploy.sh
```

---

## 11. 기술 스택 요약

| 분류 | 기술 | 버전/설명 |
|------|------|---------|
| **Backend** | Python | 3.11 |
| | FastAPI | 비동기 REST API + WebSocket |
| | uvicorn | ASGI 서버 |
| | kubernetes-client | K8s API 통신 |
| | bcrypt | 관리자 비밀번호 해싱 |
| | slowapi | Rate Limiting |
| | cryptography | RSA 키 생성 |
| **Frontend** | xterm.js 5.3 | 브라우저 터미널 |
| | Vanilla JS | 프레임워크 없음 |
| **Infrastructure** | Kubernetes | 1.24+ (SA Token Secret 방식) |
| | Helm | v3 (차트 배포) |
| | Nginx | alpine (UI 서버) |
| **Storage** | Kubernetes ConfigMap | 세션 데이터 |
| | Kubernetes Secret | 클러스터 kubeconfig, SSH 키, 자격증명 |
| **Container** | playground-runner | Ubuntu + sshd + kubectl |
| **Protocol** | WebSocket | 브라우저 터미널 |
| | SSH | 외부 터미널 직접 접속 |
| | HTTP Basic Auth | 관리자 인증 |
