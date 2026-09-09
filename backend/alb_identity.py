"""Verification for ALB-signed Cognito user claims."""
import re
import threading
import time
from urllib.request import Request, urlopen

import jwt

from .config import AWS_REGION, TRUSTED_ALB_ARN


_KEYS = {}
_LOCK = threading.Lock()
_KID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def _public_key(kid: str) -> str:
    if not _KID.fullmatch(kid):
        raise ValueError("Invalid ALB signing-key identifier")
    now=time.time()
    with _LOCK:
        cached=_KEYS.get(kid)
        if cached and cached[1] > now: return cached[0]
    url=f"https://public-keys.auth.elb.{AWS_REGION}.amazonaws.com/{kid}"
    request=Request(url,headers={"Accept":"text/plain","User-Agent":"NiveshAI/3.5"})
    with urlopen(request,timeout=5) as response:
        key=response.read(16_384).decode("ascii")
    if "BEGIN PUBLIC KEY" not in key:
        raise ValueError("Invalid ALB public key")
    with _LOCK: _KEYS[kid]=(key,now+3600)
    return key


def verify_alb_claims(encoded: str) -> dict:
    if not encoded or not TRUSTED_ALB_ARN:
        raise ValueError("Missing trusted ALB identity")
    header=jwt.get_unverified_header(encoded)
    if header.get("alg") != "ES256" or header.get("signer") != TRUSTED_ALB_ARN:
        raise ValueError("Untrusted ALB claim signer")
    claims=jwt.decode(encoded,_public_key(str(header.get("kid",""))),algorithms=["ES256"],
                      options={"verify_aud":False,"require":["exp","sub","email"]})
    verified=claims.get("email_verified")
    if verified not in {True,"true","True"}:
        raise ValueError("Verified email is required")
    if not str(claims.get("sub","")).strip() or "@" not in str(claims.get("email","")):
        raise ValueError("Incomplete Cognito identity")
    return claims
