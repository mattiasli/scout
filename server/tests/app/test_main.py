"""The ``scout-server`` entry point: refusing bad settings and dropping root in the container."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scout import __main__ as entry


def test_bad_settings_exit_with_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("SCOUT_DEV", "SCOUT_SECRET_KEY", "SCOUT_PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as exit_info:
        entry.main()
    assert exit_info.value.code == 2
    assert capsys.readouterr().err.startswith("scout-server: ")


def test_drop_root_does_nothing_without_run_as(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SCOUT_RUN_AS", raising=False)
    entry.drop_root(tmp_path / "data")
    assert not (tmp_path / "data").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the container runs Linux")
def test_drop_root_hands_over_the_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pwd

    data = tmp_path / "data"
    (data / "captures" / "abc").mkdir(parents=True)
    (data / "scout.db").write_bytes(b"")
    (data / "captures" / "abc" / "001.jpg").write_bytes(b"")
    chowned: list[str] = []
    switched: list[tuple[str, object]] = []
    monkeypatch.setenv("SCOUT_RUN_AS", "scout")
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=4242, pw_gid=4343, pw_dir="/app")
    )
    monkeypatch.setattr(os, "lchown", lambda path, uid, gid: chowned.append(os.path.relpath(path, data)))
    monkeypatch.setattr(os, "setgroups", lambda groups: switched.append(("groups", groups)))
    monkeypatch.setattr(os, "setgid", lambda gid: switched.append(("gid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: switched.append(("uid", uid)))

    entry.drop_root(data)

    assert sorted(chowned) == sorted(
        [
            ".",
            "captures",
            "scout.db",
            os.path.join("captures", "abc"),
            os.path.join("captures", "abc", "001.jpg"),
        ]
    )
    # Groups and group id go first: after setuid there's no permission left to change them.
    assert switched == [("groups", []), ("gid", 4343), ("uid", 4242)]
    assert os.environ["HOME"] == "/app"


def test_ordinary_logs_go_to_stdout_and_problems_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    logger = logging.getLogger("scout.test_main")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handlers = entry.log_handlers()
    for handler in handlers:
        logger.addHandler(handler)
    try:
        logger.info("all fine")
        logger.warning("look at this")
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
    out, err = capsys.readouterr()
    assert "INFO scout.test_main: all fine" in out
    assert "look at this" not in out
    assert "WARNING scout.test_main: look at this" in err
    assert "all fine" not in err
