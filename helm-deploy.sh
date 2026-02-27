#!/bin/bash
# helm-deploy.sh — Kubernetes Playground Platform Helm 배포 스크립트
#
# 역할:
#   - Python3로 관리자 비밀번호의 bcrypt 해시를 동적으로 생성한다.
#   - Helm upgrade --install로 playground-platform 차트를 배포/업데이트한다.
#   - 민감한 자격증명(비밀번호 해시)을 --set-string으로 직접 전달하여
#     values.yaml에 평문으로 남기지 않는다.
#
# 사전 요구사항:
#   - kubectl이 대상 클러스터를 가리키도록 설정되어 있어야 한다.
#   - helm v3이 설치되어 있어야 한다.
#   - Python3와 bcrypt 패키지가 설치되어 있어야 한다.
#     (pip install bcrypt)
#
# 사용법:
#   ./helm-deploy.sh
#   ADMIN_PASS=mypassword ./helm-deploy.sh
#   ADMIN_PASS=mypassword NAMESPACE_PLATFORM=my-ns ./helm-deploy.sh

set -e  # 명령 실패 시 즉시 종료

echo "=== Kubernetes Playground Platform Helm Deployment ==="

# ── 사전 점검 ──────────────────────────────────────────────────
# helm과 python3 바이너리가 경로에 있는지 확인한다.
command -v helm >/dev/null 2>&1 || { echo "[ERROR] helm not found. Aborting."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 not found. Aborting."; exit 1; }

# ── 환경변수 처리 ───────────────────────────────────────────────
ADMIN_USER="${ADMIN_USER:-admin}"

# ADMIN_PASS가 설정되지 않으면 즉시 오류를 출력하고 종료한다.
# 기본 비밀번호를 허용하지 않아 실수로 취약한 상태로 배포되는 것을 방지한다.
ADMIN_PASS="${ADMIN_PASS:?ERROR: ADMIN_PASS environment variable must be set. Please provide a secure password.}"

NAMESPACE_PLATFORM="${NAMESPACE_PLATFORM:-playground-system}"
RELEASE_NAME="playground-platform"

echo ""
echo "[1/3] Configuration:"
echo "  Release Name : $RELEASE_NAME"
echo "  Namespace    : $NAMESPACE_PLATFORM"
echo "  Admin user   : $ADMIN_USER"
echo ""

# ── bcrypt 해시 동적 생성 ──────────────────────────────────────
# bcrypt 해시를 배포 시점에 즉석으로 생성하여 values.yaml에 평문 비밀번호가
# 저장되지 않도록 한다. 해시는 salt가 포함되어 있어 같은 비밀번호라도
# 매번 다른 해시가 생성된다(replay attack 방지).
echo "[2/3] Generating secure bcrypt hash..."
ADMIN_HASH=$(python3 -c "import bcrypt, sys; pw=sys.argv[1].encode(); print(bcrypt.hashpw(pw, bcrypt.gensalt()).decode())" "$ADMIN_PASS")

# ── Helm 배포 ──────────────────────────────────────────────────
# upgrade --install: 릴리스가 없으면 install, 있으면 upgrade로 동작 (멱등성)
# --create-namespace: 네임스페이스가 없으면 자동 생성
# --set-string: 비밀번호 해시에 특수문자($, #)가 포함되어 있으므로 string 타입으로 전달
# "$@": 스크립트 실행 시 추가로 전달된 인수를 helm 명령에 그대로 전달 (유연한 커스터마이징)
echo ""
echo "[3/3] Executing Helm Upgrade/Install..."

helm upgrade --install "$RELEASE_NAME" ./chart/playground-platform \
  --namespace "$NAMESPACE_PLATFORM" \
  --create-namespace \
  --set adminUser="$ADMIN_USER" \
  --set-string adminPasswordHash="$ADMIN_HASH" \
  "$@"

echo ""
echo "============================================"
echo "=== Helm Deployment Complete! =============="
echo "============================================"
echo "릴리스 상태 확인:"
echo "  helm status $RELEASE_NAME -n $NAMESPACE_PLATFORM"
echo ""
echo "파드 상태 확인:"
echo "  kubectl get pods -n $NAMESPACE_PLATFORM"
