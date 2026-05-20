from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import os, jwt as pyjwt
from datetime import datetime, timedelta, timezone

router = APIRouter()

# ── HTTP Bearer extractor (auto_error=False → จัดการเองใน Depends) ──
_bearer = HTTPBearer(auto_error=False)


def _secret() -> str:
    return os.getenv("JWT_SECRET", "insurance-default-secret-please-change-in-prod")


def make_token(username: str) -> str:
    return pyjwt.encode(
        {
            "sub": username,
            "exp": datetime.now(tz=timezone.utc) + timedelta(days=30),
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


# ── Public endpoints ────────────────────────────────────────────────

@router.post("/auth/login")
async def login(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="ข้อมูลไม่ถูกต้อง")

    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="กรุณากรอกชื่อผู้ใช้และรหัสผ่าน")

    valid_user = os.getenv("APP_USERNAME", "admin")
    valid_pass = os.getenv("APP_PASSWORD", "admin1234")

    if username != valid_user or password != valid_pass:
        raise HTTPException(status_code=401, detail="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")

    token = make_token(username)
    return {"token": token, "username": username}
