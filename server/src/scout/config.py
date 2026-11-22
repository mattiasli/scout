"""Server configuration, read from environment variables.

Secrets never appear in ``repr`` output, so a logged ``Config`` is safe.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MIN_SECRET_LENGTH = 32


class ConfigError(Exception):
    """The environment doesn't describe a usable configuration. The message says what to fix."""


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


@dataclass(frozen=True)
class Config:
    public_url: str
    secret_key: str = field(repr=False)
    data_dir: Path
    port: int = 8000
    dev: bool = False
    github_client_id: str | None = None
    github_client_secret: str | None = field(default=None, repr=False)
    owner_github_id: int | None = None
    bootstrap_token: str | None = field(default=None, repr=False)
    bootstrap_car_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        parts = urlsplit(self.public_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ConfigError(
                f"SCOUT_PUBLIC_URL must look like https://example.com, not {self.public_url!r}."
            )
        if parts.scheme != "https" and not self.dev:
            raise ConfigError(
                "SCOUT_PUBLIC_URL must use https:// (plain http:// only works with SCOUT_DEV=1)."
            )
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ConfigError("SCOUT_PUBLIC_URL must be just the scheme and host, without a path.")
        object.__setattr__(self, "public_url", f"{parts.scheme}://{parts.netloc}")
        if len(self.secret_key) < MIN_SECRET_LENGTH:
            raise ConfigError(f"SCOUT_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters long.")
        if self.bootstrap_token is not None and len(self.bootstrap_token) < MIN_SECRET_LENGTH:
            raise ConfigError(f"SCOUT_BOOTSTRAP_TOKEN must be at least {MIN_SECRET_LENGTH} characters long.")
        if self.bootstrap_token is not None and self.owner_github_id is None:
            raise ConfigError(
                "SCOUT_BOOTSTRAP_TOKEN belongs to the owner, so SCOUT_OWNER_GITHUB_ID must be set too."
            )
        if self.bootstrap_car_key is not None and len(self.bootstrap_car_key) < 24:
            raise ConfigError("SCOUT_BOOTSTRAP_CAR_KEY must be at least 24 characters long.")

    @property
    def github_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def public_host(self) -> str:
        """The host (and port, if any) of the public URL, as it appears in a Host header."""
        return urlsplit(self.public_url).netloc

    @property
    def car_socket_url(self) -> str:
        scheme = "wss" if self.public_url.startswith("https://") else "ws"
        return f"{scheme}://{self.public_host}/car/ws"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scout.db"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        dev = _flag(env.get("SCOUT_DEV"))

        port_text = _clean(env.get("PORT")) or "8000"
        try:
            port = int(port_text)
        except ValueError:
            raise ConfigError(f"PORT must be a number, not {port_text!r}.") from None

        public_url = _clean(env.get("SCOUT_PUBLIC_URL"))
        if public_url is None and _clean(env.get("RAILWAY_PUBLIC_DOMAIN")):
            public_url = f"https://{_clean(env.get('RAILWAY_PUBLIC_DOMAIN'))}"
        if public_url is None:
            if not dev:
                raise ConfigError(
                    "Set SCOUT_PUBLIC_URL, for example https://scout-production.up.railway.app."
                )
            public_url = f"http://127.0.0.1:{port}"

        secret_key = _clean(env.get("SCOUT_SECRET_KEY"))
        if secret_key is None:
            if not dev:
                raise ConfigError(
                    "Set SCOUT_SECRET_KEY to a long random value, for example the output of "
                    'python -c "import secrets; print(secrets.token_urlsafe(48))".'
                )
            # Development only: sign-ins last until the server restarts.
            secret_key = secrets.token_urlsafe(48)

        data_dir = (
            _clean(env.get("SCOUT_DATA_DIR")) or _clean(env.get("RAILWAY_VOLUME_MOUNT_PATH")) or "./data"
        )

        owner_text = _clean(env.get("SCOUT_OWNER_GITHUB_ID"))
        owner_id: int | None = None
        if owner_text is not None:
            if not owner_text.isdigit() or int(owner_text) <= 0:
                raise ConfigError(
                    "SCOUT_OWNER_GITHUB_ID must be the numeric GitHub user id "
                    "(see https://api.github.com/users/<login>), not the login."
                )
            owner_id = int(owner_text)

        return cls(
            public_url=public_url,
            secret_key=secret_key,
            data_dir=Path(data_dir),
            port=port,
            dev=dev,
            github_client_id=_clean(env.get("GITHUB_CLIENT_ID")),
            github_client_secret=_clean(env.get("GITHUB_CLIENT_SECRET")),
            owner_github_id=owner_id,
            bootstrap_token=_clean(env.get("SCOUT_BOOTSTRAP_TOKEN")),
            bootstrap_car_key=_clean(env.get("SCOUT_BOOTSTRAP_CAR_KEY")),
        )
