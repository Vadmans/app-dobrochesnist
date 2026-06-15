import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from config import SECRET_KEY, SESSION_COOKIE, SESSION_TTL_SECONDS
from database import get_db
from models import AdminUser


MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCK_SECONDS = 300
_login_fails: dict = {}

def login_lock_remaining(key: str) -> int:
    rec = _login_fails.get(key)
    if not rec:
        return 0
    _, lock_until = rec
    remaining = int(lock_until - time.time())
    return remaining if remaining > 0 else 0

def login_register_fail(key: str) -> None:
    count, lock_until = _login_fails.get(key, (0, 0.0))
    count += 1
    if count >= MAX_LOGIN_ATTEMPTS:
        lock_until = time.time() + LOGIN_LOCK_SECONDS
        count = 0
    _login_fails[key] = (count, lock_until)

def login_reset(key: str) -> None:
    _login_fails.pop(key, None)

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return base64.b64encode(digest).decode("utf-8"), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    calculated, _ = hash_password(password, salt)
    return secrets.compare_digest(calculated, password_hash)


def sign_value(value: str) -> str:
    sig = hmac.new(SECRET_KEY.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def unsign_value(signed: str) -> Optional[str]:
    if not signed or "." not in signed:
        return None
    value, sig = signed.rsplit(".", 1)
    expected = hmac.new(SECRET_KEY.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return value if secrets.compare_digest(sig, expected) else None


def create_session_cookie(user_id: str) -> str:
    expires = int(datetime.now(timezone.utc).timestamp()) + SESSION_TTL_SECONDS
    return sign_value(f"{user_id}:{expires}")


def get_session_user(request: Request, db: Session) -> Optional[AdminUser]:
    raw = unsign_value(request.cookies.get(SESSION_COOKIE) or "")
    if not raw:
        return None
    try:
        user_id, expires_s = raw.split(":", 1)
        if int(expires_s) < int(datetime.now(timezone.utc).timestamp()):
            return None
    except Exception:
        return None
    return db.get(AdminUser, user_id)


def require_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    user = get_session_user(request, db)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Потрібна авторизація")
    return user


def admin_exists(db: Session) -> bool:
    return db.query(AdminUser).count() > 0


