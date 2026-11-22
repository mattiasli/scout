"""``scout-server``: run the Scout server with uvicorn."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

from scout.app import create_app
from scout.config import Config, ConfigError
from scout.security import install_log_redaction

log = logging.getLogger("scout")


def drop_root(data_dir: Path) -> None:
    """Hand the data directory to an unprivileged user, then become that user.

    Railway mounts volumes owned by root, so the container starts as root. When
    ``SCOUT_RUN_AS`` names a user (the Dockerfile sets ``scout``), the server takes
    ownership of the data directory and switches to that user before it opens the
    database or a socket. Without ``SCOUT_RUN_AS``, or when not root, nothing changes.
    """
    if sys.platform == "win32":
        return
    run_as = os.environ.get("SCOUT_RUN_AS", "").strip()
    if not run_as or os.geteuid() != 0:
        return
    import pwd

    entry = pwd.getpwnam(run_as)
    uid, gid = entry.pw_uid, entry.pw_gid
    data_dir.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(data_dir):
        # Each directory comes up once as ``root``. Symlinks to directories are listed in
        # ``dirs`` but never walked, so they are handled here like files.
        links = [name for name in dirs if os.path.islink(os.path.join(root, name))]
        for path in [root, *(os.path.join(root, name) for name in (*files, *links))]:
            if os.lstat(path).st_uid != uid:
                os.lchown(path, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = entry.pw_dir
    log.info("Running as %s; %s belongs to it", run_as, data_dir)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"scout-server: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    drop_root(config.data_dir)
    # The SDK logs every stateless request at INFO; that's noise here.
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    if config.dev:
        log.warning("Development mode (SCOUT_DEV=1): insecure cookies and ws:// allowed.")
    app = create_app(config)
    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=config.port,
        # Railway terminates TLS and forwards the original scheme and client address.
        proxy_headers=True,
        forwarded_allow_ips="*",
        # Protocol-level pings find dead car links (the car link also pings every 5 s).
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        ws_max_size=8 * 1024 * 1024,
        timeout_keep_alive=30,
        timeout_graceful_shutdown=10,
        server_header=False,
        log_level="info",
    )
    # uvicorn.Config sets up its loggers; add the token redaction on top of that.
    install_log_redaction()
    uvicorn.Server(server_config).run()


if __name__ == "__main__":
    main()
