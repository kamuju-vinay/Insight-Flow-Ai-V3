import os
import time
import bcrypt
import jwt as pyjwt
from fastapi import Header, HTTPException

# =============================================================================
# auth.py — real authentication for Insight Flow AI
#
# The frontend (src/api.js, src/LoginPage.jsx) was already built expecting
# real per-user login: it stores a token and sends it as
# `Authorization: Bearer <token>` on every request. Previously the backend's
# /api/auth/login and /api/auth/signup just returned a fixed fake token for
# any input, and no endpoint ever checked it — so every user saw the same
# shared data. This module makes that real.
#
# REQUIRED ENV VAR:
#   JWT_SECRET  - any long random string, used to sign login tokens.
#                 Set this in Railway → Variables. If you don't set it, a
#                 random one is generated at startup as a fallback — which
#                 works, but logs everyone out on every restart, so you
#                 should set a real one.
# =============================================================================

JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    import secrets
    JWT_SECRET = secrets.token_hex(32)
    print(
        "⚠️  JWT_SECRET is not set — using a random secret generated at startup. "
        "This means everyone will be logged out the next time the server restarts. "
        "Set JWT_SECRET in Railway → Variables to a long random string to fix this."
    )

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 60 * 60 * 24 * 30  # 30 days


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def create_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRY_SECONDS,
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str):
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None


def get_current_user_id(authorization: str = Header(None)) -> str:
    """FastAPI dependency — every protected endpoint takes
    `user_id: str = Depends(get_current_user_id)` and gets back the caller's
    own user id, or a 401 if the token is missing/invalid/expired (which the
    frontend's apiFetch already handles by clearing the token and reloading
    to the login screen)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[len("Bearer "):]
    payload = decode_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return payload["sub"]
