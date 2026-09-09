import base64
import hashlib
import hmac
import json
import os
import time
import secrets
from typing import Dict

from .config import JWT_SECRET, TOKEN_TTL_SECONDS

PBKDF2_ITERATIONS = 600_000
TOKEN_ISSUER = "nivesh-ai"
TOKEN_AUDIENCE = "nivesh-web"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        parts = encoded.split("$")
        if len(parts) == 3:  # backward-compatible verification of existing local accounts
            _, salt, expected = parts
            iterations = 210_000
        elif len(parts) == 4:
            _, raw_iterations, salt, expected = parts
            iterations = int(raw_iterations)
        else:
            return False
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), iterations)
        return hmac.compare_digest(_b64(actual), expected)
    except (ValueError, TypeError):
        return False


def password_needs_rehash(encoded: str) -> bool:
    try:
        parts = encoded.split("$")
        return len(parts) != 4 or int(parts[1]) < PBKDF2_ITERATIONS
    except (ValueError, TypeError):
        return True


def create_token(user_id: int, email: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64(json.dumps({"sub": user_id, "email": email, "iat": now, "exp": now + TOKEN_TTL_SECONDS,
                               "iss": TOKEN_ISSUER, "aud": TOKEN_AUDIENCE, "jti": secrets.token_urlsafe(24)}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> Dict:
    try:
        header, payload, signature = token.split(".")
        decoded_header = json.loads(_unb64(header))
        if decoded_header != {"alg": "HS256", "typ": "JWT"}:
            raise ValueError("Unsupported token header")
        expected = _b64(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid signature")
        claims = json.loads(_unb64(payload))
        now = time.time()
        if claims.get("iss") != TOKEN_ISSUER or claims.get("aud") != TOKEN_AUDIENCE:
            raise ValueError("Invalid token scope")
        if not isinstance(claims.get("sub"), int) or not claims.get("jti"):
            raise ValueError("Invalid token claims")
        if claims.get("iat", now + 1) > now + 30 or claims.get("exp", 0) < now:
            raise ValueError("Token expired")
        return claims
    except Exception as exc:
        raise ValueError("Invalid or expired token") from exc


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
