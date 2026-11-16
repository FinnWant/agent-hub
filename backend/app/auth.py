"""Single admin login: password hashing, signed session cookie, CSRF and rate limiting.

Every endpoint except login and health depends on `require_admin`. With no
ADMIN_PASSWORD_HASH or SESSION_SECRET configured, everything fails closed (503).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket

from . import config

COOKIE = "ah_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "agent-hub"

# scrypt cost: ~50 ms and 32 MiB per hash, so brute-forcing a stolen hash is expensive.
_N, _R, _P = 2**15, 8, 1


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 2**20)
    # ":" separators, not "$": systemd's EnvironmentFile would try to expand "$..."
    return f"scrypt:{_N}:{_R}:{_P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split(":")
        if algo != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                maxmem=64 * 2**20, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# DEMO_MODE without a configured password: log in with "demo". Demo sandboxes are fake.
_demo_hash: str | None = None
_demo_secret = secrets.token_hex(32)


def _credentials() -> tuple[str, str]:
    global _demo_hash
    s = config.settings
    if s.demo_mode and not s.admin_password_hash:
        _demo_hash = _demo_hash or hash_password("demo")
        return _demo_hash, s.session_secret or _demo_secret
    return s.admin_password_hash, s.session_secret


def check_password(password: str) -> bool:
    return verify_password(password, _credentials()[0])


def configured() -> bool:
    pw_hash, secret = _credentials()
    return bool(pw_hash and secret)


# ---------------------------------------------------------------------------
# Session tokens: "<expiry>.<nonce>.<hmac>", signed with SESSION_SECRET. The password hash is
# part of the signed data, so setting a new password logs every session out; logging out
# records the session's signature so that one token stops working.
# ---------------------------------------------------------------------------

def _sign(expiry: int, nonce: str) -> str:
    pw_hash, secret = _credentials()
    return hmac.new(secret.encode(), f"{expiry}|{nonce}|{pw_hash}".encode(), hashlib.sha256).hexdigest()


def make_token() -> tuple[str, int]:
    max_age = config.settings.session_days * 86400
    expiry = int(time.time()) + max_age
    nonce = secrets.token_hex(8)
    return f"{expiry}.{nonce}.{_sign(expiry, nonce)}", max_age


def token_parts(token: str | None) -> tuple[str, int] | None:
    """(signature, expiry) of a well-formed token, else None."""
    parts = (token or "").split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1] or not parts[2]:
        return None
    return parts[2], int(parts[0])


def token_valid(token: str | None, revoked=None) -> bool:
    """`revoked(sig)` says whether that session was ended by logging out."""
    if not token or not configured() or not (parts := token_parts(token)):
        return False
    sig, expiry = parts
    if expiry < time.time() or not hmac.compare_digest(sig, _sign(expiry, token.split(".")[1])):
        return False
    return not (revoked and revoked(sig))


def _revoked(conn: Request | WebSocket):
    app = getattr(conn, "app", None)
    svc = getattr(getattr(app, "state", None), "service", None)
    return svc.db.session_revoked if svc is not None else None


# ---------------------------------------------------------------------------
# Login rate limiting: 5 failures per 15 minutes per client IP (in memory).
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, limit: int = 5, window: float = 900) -> None:
        self.limit = limit
        self.window = window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque[float]:
        q = self._fails[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def retry_after(self, key: str) -> int:
        """Seconds until `key` may try again (0 = allowed now)."""
        now = time.monotonic()
        q = self._trim(key, now)
        return int(q[0] + self.window - now) + 1 if len(q) >= self.limit else 0

    def fail(self, key: str) -> None:
        self._fails[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


login_limiter = RateLimiter()


LOOPBACK = {"127.0.0.1", "::1"}


def client_ip(conn: Request | WebSocket) -> str:
    """Who is calling, for the audit log and the login rate limit. Behind `tailscale serve`
    the peer is always 127.0.0.1, so use the forwarded address and Tailscale login instead
    (only when the request really came from the local proxy)."""
    peer = conn.client.host if conn.client else "unknown"
    if not (config.settings.trust_proxy and peer in LOOPBACK):
        return peer
    forwarded = (conn.headers.get("x-forwarded-for") or "").split(",")[-1].strip()
    login = (conn.headers.get("tailscale-user-login") or "").strip()[:100]
    who = forwarded or peer
    return f"{who} ({login})" if login else who


def allowed_origins(conn: Request | WebSocket) -> set[str]:
    """Origins a browser may legitimately send: the public URL, and the Host it connected to."""
    out = set()
    if config.settings.public_url:
        u = urlsplit(config.settings.public_url)
        out.add(f"{u.scheme}://{u.netloc}")
    host = conn.headers.get("host")
    if host:
        out |= {f"http://{host}", f"https://{host}"}
    return out


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def on_preview_host(conn: Request | WebSocket) -> bool:
    """True when the request came in on the hostname reserved for previews. Kiln never
    issues or accepts a session there, so preview servers never see one."""
    host = (conn.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower().rstrip(".")
    preview = config.settings.preview_host.lower().rstrip(".")
    return bool(preview) and host == preview


def require_not_preview_host(request: Request) -> None:
    if on_preview_host(request):
        raise HTTPException(403, "This hostname is reserved for previews. Open Kiln at its usual address instead.")


def require_configured() -> None:
    if not configured():
        raise HTTPException(503, "Admin login isn't set up. Run: python -m app.set_password")


def require_csrf(request: Request) -> None:
    """Browsers can't send a custom header cross-site without a CORS preflight we never allow."""
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, f"Missing CSRF header ({CSRF_HEADER}: {CSRF_VALUE})")


def require_admin(request: Request) -> None:
    require_not_preview_host(request)
    require_configured()
    if request.method not in ("GET", "HEAD"):
        require_csrf(request)
    if not token_valid(request.cookies.get(COOKIE), _revoked(request)):
        raise HTTPException(401, "Log in first")


def ws_allowed(ws: WebSocket) -> bool:
    """Cookie plus a same-origin check: WebSockets ignore SameSite on some browsers and have no CORS."""
    origin = ws.headers.get("origin")
    if not origin or origin not in allowed_origins(ws) or on_preview_host(ws):
        return False
    return token_valid(ws.cookies.get(COOKIE), _revoked(ws))
