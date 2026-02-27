import os
import secrets
import bcrypt
import uuid
import time
from typing import Optional
from fastapi import Depends, HTTPException, status, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from dotenv import load_dotenv

# .env 파일이 있으면 환경변수로 로드 (로컬 개발 환경 편의용)
load_dotenv()

# 인메모리 관리자 세션 저장소 (pod 재시작 시 초기화됨 - 보안상 더 안전)
# session_id -> {"username": str, "expires_at": float}
ADMIN_SESSIONS = {}

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

if not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_PASSWORD_HASH must be set via environment variable.")


def check_admin_credentials(username: str, password: str) -> bool:
    """사용자명과 비밀번호 검증"""
    if not username or not password:
        return False
    correct_username = secrets.compare_digest(
        username.encode("utf-8"),
        ADMIN_USER.encode("utf-8"),
    )
    try:
        correct_password = bcrypt.checkpw(
            password.encode("utf-8"),
            ADMIN_PASSWORD_HASH.encode("utf-8"),
        )
    except Exception:
        return False
    return correct_username and correct_password


def create_admin_session(username: str) -> str:
    """새 세션을 생성하고 ID를 반환한다."""
    session_id = str(uuid.uuid4())
    # 세션 만료 기간은 12시간으로 설정
    ADMIN_SESSIONS[session_id] = {
        "username": username,
        "expires_at": time.time() + (12 * 3600),
    }
    return session_id


def delete_admin_session(session_id: str):
    """세션을 파기한다."""
    if session_id in ADMIN_SESSIONS:
        del ADMIN_SESSIONS[session_id]


async def verify_admin_session(request: Request) -> str:
    """
    쿠키에서 admin_session을 확인하여 인증한다.
    실패 시 401 에러를 던진다.
    """
    session_id = request.cookies.get("admin_session")
    if not session_id or session_id not in ADMIN_SESSIONS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin session expired or invalid",
        )

    session = ADMIN_SESSIONS[session_id]
    if time.time() > session["expires_at"]:
        delete_admin_session(session_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin session expired",
        )

    return session["username"]


# 하위 호환성을 위해 남겨두되, 점진적으로 verify_admin_session으로 교체 권장
security = HTTPBasic()


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """HTTP Basic Auth 기반 (레거시 대응용)"""
    if not check_admin_credentials(credentials.username, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect admin username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
