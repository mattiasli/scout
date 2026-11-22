"""Jinja2 setup: autoescaping, and filters for times and durations."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import jinja2
from starlette.templating import Jinja2Templates

from scout import __version__
from scout.config import Config


def when(ts: float | None) -> str:
    """A unix time as a short UTC date and time."""
    if ts is None:
        return "never"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def ago(ts: float | None, now: float | None = None) -> str:
    """A unix time as "3 min ago"."""
    if ts is None:
        return "never"
    seconds = max(0, int((time.time() if now is None else now) - ts))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def make_templates(directory: Path, config: Config) -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(directory)),
        autoescape=jinja2.select_autoescape(["html"], default_for_string=True, default=True),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["when"] = when
    env.filters["ago"] = ago
    env.globals["public_url"] = config.public_url
    env.globals["mcp_url"] = f"{config.public_url}/mcp"
    env.globals["car_socket_url"] = config.car_socket_url
    env.globals["version"] = __version__
    env.globals["github_configured"] = config.github_configured
    return Jinja2Templates(env=env)
