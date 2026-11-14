"""인증·세션·CSRF·요청 속도 제한."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict, deque

SESSION_TTL = 60 * 60 * 24 * 14  # 14일


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$14$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                              n=2 ** int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


def _sign(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def make_token(secret: str, pw_marker: str) -> str:
    """서명된 무상태 세션 토큰. 서버가 재시작되어도 SECRET_KEY가 같으면 유효하다.
    pw_marker(비밀번호 해시의 일부)가 바뀌면 기존 토큰은 모두 무효가 된다."""
    body = base64.urlsafe_b64encode(json.dumps(
        {"sid": base64.urlsafe_b64encode(os.urandom(12)).decode(), "exp": int(time.time()) + SESSION_TTL,
         "pm": hashlib.sha256(pw_marker.encode()).hexdigest()[:12]}).encode()).decode()
    return body + "." + _sign(secret, body.encode())


def read_token(secret: str, token: str, pw_marker: str):
    try:
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(secret, body.encode())):
            return None
        data = json.loads(base64.urlsafe_b64decode(body))
        if data["exp"] < time.time():
            return None
        if data["pm"] != hashlib.sha256(pw_marker.encode()).hexdigest()[:12]:
            return None
        return data
    except Exception:
        return None


def csrf_for(secret: str, sid: str) -> str:
    return _sign(secret, ("csrf:" + sid).encode())[:32]


class RateLimiter:
    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self.hits) > 5000:  # 메모리 보호
            self.hits.clear()
        return True
