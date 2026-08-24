from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import hmac
import os
import time
import jwt as pyjwt
from datetime import datetime, timedelta, timezone

router = APIRouter()

# ── HTTP Bearer extractor (auto_error=False → จัดการเองใน Depends) ──
_bearer = HTTPBearer(auto_error=False)


_LOGIN_WINDOW_SECONDS = 15 * 60
_MAX_FAILED_ATTEMPTS = 5
_failed_attempts: dict[str, list[float]] = {}


def _secret() -> str:
    """JWT secret must always be configured outside source control."""
    secret = os.getenv("JWT_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="ระบบยืนยันตัวตนยังไม่ได้ตั้งค่า")
    return secret


def make_token(username: str, remember: bool = True) -> str:
    lifetime = timedelta(days=30) if remember else timedelta(hours=8)
    return pyjwt.encode(
        {
            "sub": username,
            "exp": datetime.now(tz=timezone.utc) + lifetime,
        },
        _secret(),
        algorithm="HS256",
    )


def verify_token(token: str) -> str:
    """Decode JWT → return username, raise 401 on failure."""
    try:
        payload = pyjwt.decode(token, _secret(), algorithms=["HS256"])
        return payload["sub"]
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session หมดอายุ กรุณาเข้าสู่ระบบใหม่")
    except Exception:
        raise HTTPException(status_code=401, detail="Token ไม่ถูกต้อง กรุณาเข้าสู่ระบบใหม่")


def require_auth(creds: HTTPAuthorizationCredentials = Security(_bearer)) -> str:
    """FastAPI Dependency — ใช้กับ router ที่ต้องการ auth"""
    if not creds:
        raise HTTPException(status_code=401, detail="กรุณาเข้าสู่ระบบก่อน")
    return verify_token(creds.credentials)


def _client_key(request: Request) -> str:
    """Use the originating address when a reverse proxy supplies it."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _retry_after(key: str) -> int:
    now = time.monotonic()
    attempts = [t for t in _failed_attempts.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
    if attempts:
        _failed_attempts[key] = attempts
    else:
        _failed_attempts.pop(key, None)
    if len(attempts) < _MAX_FAILED_ATTEMPTS:
        return 0
    return max(1, int(_LOGIN_WINDOW_SECONDS - (now - attempts[0])))


def _record_failed_attempt(key: str) -> None:
    now = time.monotonic()
    attempts = [t for t in _failed_attempts.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
    attempts.append(now)
    _failed_attempts[key] = attempts


# ── Public endpoints ────────────────────────────────────────────────

@router.post("/auth/login")
async def login(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="ข้อมูลไม่ถูกต้อง")

    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    remember = bool(body.get("remember", True))

    if not username or not password:
        raise HTTPException(status_code=400, detail="กรุณากรอกชื่อผู้ใช้และรหัสผ่าน")

    valid_user = os.getenv("APP_USERNAME", "").strip()
    valid_pass = os.getenv("APP_PASSWORD", "")
    if not valid_user or not valid_pass:
        raise HTTPException(status_code=503, detail="ระบบยืนยันตัวตนยังไม่ได้ตั้งค่า")

    client_key = _client_key(request)
    retry_after = _retry_after(client_key)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="ลองรหัสผ่านไม่สำเร็จหลายครั้ง กรุณารอสักครู่แล้วลองใหม่",
            headers={"Retry-After": str(retry_after)},
        )

    # username เทียบแบบ case-insensitive และใช้ constant-time compare กับรหัสผ่าน
    user_matches = hmac.compare_digest(username.casefold().encode(), valid_user.casefold().encode())
    password_matches = hmac.compare_digest(password.encode(), valid_pass.encode())
    if not user_matches or not password_matches:
        _record_failed_attempt(client_key)
        raise HTTPException(status_code=401, detail="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")

    _failed_attempts.pop(client_key, None)
    token = make_token(valid_user, remember)
    return {
        "token": token,
        "username": valid_user,
        "expires_in": 30 * 24 * 60 * 60 if remember else 8 * 60 * 60,
    }
