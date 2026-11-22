"""Small security helpers: secret generation and hashing, signed links, CSRF, response
headers and log redaction."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOKEN_PREFIX = "scout_"
CAR_KEY_PREFIX = "scoutcar_"
SHOWN_PREFIX_LENGTH = 12  # how much of a token or key is kept to recognise it later


def new_token() -> str:
    """A personal MCP token: ``scout_`` plus 32 bytes of URL-safe randomness."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_car_key() -> str:
    return CAR_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    """SHA-256 hex digest. Tokens and keys are long random strings, so a fast hash is right."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def shown_prefix(value: str) -> str:
    return value[:SHOWN_PREFIX_LENGTH]


def same_secret(a: str, b: str) -> bool:
    """Constant-time comparison of two secrets."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def derive_key(secret_key: str, purpose: str) -> bytes:
    """A separate key per purpose, so a signature made for one thing never fits another."""
    return hmac.new(secret_key.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).digest()


class LinkSigner:
    """Signs short strings with an expiry, for links that work without signing in."""

    def __init__(self, secret_key: str, purpose: str) -> None:
        self._key = derive_key(secret_key, f"scout link signer: {purpose}")

    def signature(self, subject: str, expires: int) -> str:
        mac = hmac.new(self._key, f"{subject}\x00{expires}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")

    def verify(self, subject: str, expires: int, signature: str, now: float) -> bool:
        if expires < now:
            return False
        return same_secret(self.signature(subject, expires), signature)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


# Content-Security-Policy for every response. The site has no scripts at all.
CSP = (
    "default-src 'none'; "
    "style-src 'self'; "
    "img-src 'self' data: https://avatars.githubusercontent.com; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)


class SecurityHeadersMiddleware:
    """Adds security headers to every HTTP response (pure ASGI, so streaming still works)."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", CSP)
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000")
            await send(message)

        await self.app(scope, receive, send_with_headers)


# Paths and query values that act as credentials. They are replaced before anything is logged.
_REDACTIONS = [
    (re.compile(r"(/mcp/)[^/?\s\"]+"), r"\1<redacted>"),
    (re.compile(r"(/i/)[^/?\s\"]+"), r"\1<redacted>"),
    (re.compile(r"([?&](?:sig|code|state)=)[^&\s\"]+"), r"\1<redacted>"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Removes tokens from request paths in log records (uvicorn's access log in particular)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        if isinstance(record.msg, str) and ("/mcp/" in record.msg or "/i/" in record.msg):
            record.msg = redact(record.msg)
        return True


def install_log_redaction() -> None:
    """Attach the redacting filter to the loggers that see request paths and to every
    handler they write through (logger filters alone don't see records from child loggers)."""
    flt = RedactingFilter()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn", "mcp", "scout", ""):
        logger = logging.getLogger(name)
        targets: list[logging.Filterer] = [logger, *logger.handlers]
        for target in targets:
            if not any(isinstance(f, RedactingFilter) for f in target.filters):
                target.addFilter(flt)
