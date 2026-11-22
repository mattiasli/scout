"""Accounts, roles, owner protection, tokens, configuration and storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from scout.config import Config, ConfigError
from scout.db import MAX_ACTIVE_TOKENS_PER_USER, MIGRATIONS, NotAllowed, Store

from .fakes import FAKE_SECRET_KEY, OWNER_GITHUB_ID, add_user


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "scout.db", owner_github_id=OWNER_GITHUB_ID)
    yield s
    s.close()


# ----------------------------------------------------------------- roles and status


def test_new_user_waits_and_is_not_admin(store: Store) -> None:
    user, created = store.sign_in_github_user(github_id=42, login="newbie", name="New Bie", avatar_url=None)
    assert created
    assert user.status == "waiting"
    assert not user.is_admin and not user.admin and not user.active


def test_owner_is_active_admin_on_first_sign_in(store: Store) -> None:
    owner, _ = store.sign_in_github_user(
        github_id=OWNER_GITHUB_ID, login="owner-login", name=None, avatar_url=None
    )
    assert owner.is_owner and owner.status == "active" and owner.admin


def test_owner_recognised_by_numeric_id_not_login(store: Store) -> None:
    impostor, _ = store.sign_in_github_user(github_id=777, login="owner-login", name=None, avatar_url=None)
    assert not impostor.is_owner and impostor.status == "waiting"


def test_sign_in_updates_profile_but_keeps_status(store: Store) -> None:
    user = add_user(store, 50, "before")
    again, created = store.sign_in_github_user(github_id=50, login="after", name="Renamed", avatar_url=None)
    assert not created
    assert again.id == user.id and again.login == "after" and again.name == "Renamed"
    assert again.status == "active"


def test_activation_never_grants_admin(store: Store) -> None:
    owner = store.ensure_owner()
    assert owner is not None
    user, _ = store.sign_in_github_user(github_id=60, login="u60", name=None, avatar_url=None)
    approved = store.set_status(user.id, "active", by=owner)
    assert approved.active and not approved.is_admin
    assert approved.approved_by == owner.id and approved.approved_at is not None


def test_admin_is_granted_separately_and_needs_an_active_account(store: Store) -> None:
    owner = store.ensure_owner()
    assert owner is not None
    user, _ = store.sign_in_github_user(github_id=61, login="u61", name=None, avatar_url=None)
    with pytest.raises(NotAllowed):
        store.set_admin(user.id, True, by=owner)
    store.set_status(user.id, "active", by=owner)
    assert store.set_admin(user.id, True, by=owner).admin
    assert not store.set_admin(user.id, False, by=owner).is_admin


def test_suspended_admin_is_not_an_admin(store: Store) -> None:
    owner = store.ensure_owner()
    assert owner is not None
    admin = add_user(store, 62, "boss", admin=True)
    suspended = store.set_status(admin.id, "suspended", by=owner)
    assert suspended.is_admin and not suspended.admin


def test_owner_cannot_be_suspended_or_demoted(store: Store) -> None:
    owner = store.ensure_owner()
    assert owner is not None
    other_admin = add_user(store, 70, "other-admin", admin=True)
    with pytest.raises(NotAllowed):
        store.set_status(owner.id, "suspended", by=other_admin)
    with pytest.raises(NotAllowed):
        store.set_admin(owner.id, False, by=other_admin)
    owner_now = store.get_user(owner.id)
    assert owner_now is not None and owner_now.admin


def test_owner_is_restored_even_if_the_database_was_changed(store: Store, tmp_path: Path) -> None:
    owner = store.ensure_owner()
    assert owner is not None
    with sqlite3.connect(tmp_path / "scout.db") as raw:
        raw.execute("UPDATE users SET status = 'suspended', is_admin = 0 WHERE id = ?", (owner.id,))
    raw.close()
    restored, _ = store.sign_in_github_user(
        github_id=OWNER_GITHUB_ID, login="owner", name=None, avatar_url=None
    )
    assert restored.admin


def test_admins_cannot_suspend_or_demote_themselves(store: Store) -> None:
    admin = add_user(store, 80, "self", admin=True)
    with pytest.raises(NotAllowed):
        store.set_status(admin.id, "suspended", by=admin)
    with pytest.raises(NotAllowed):
        store.set_admin(admin.id, False, by=admin)


# --------------------------------------------------------------------------- tokens


def test_token_lifecycle(store: Store, tmp_path: Path) -> None:
    user = add_user(store, 90, "tokens")
    token, plain = store.create_token(user.id, "  Claude   Code  ")
    assert plain.startswith("scout_") and len(plain) >= len("scout_") + 43
    assert token.label == "Claude Code"
    assert plain.startswith(token.prefix) and len(token.prefix) < len(plain)

    found = store.find_token(plain)
    assert found is not None and found.user_id == user.id
    assert store.list_tokens(user.id)[0].last_used_at is not None

    # Only a hash is stored.
    raw = sqlite3.connect(tmp_path / "scout.db")
    dump = "\n".join(raw.iterdump())
    raw.close()
    assert plain not in dump

    assert store.revoke_token(user.id, token.id)
    assert store.find_token(plain) is None
    assert store.list_tokens(user.id) == []
    assert not store.revoke_token(user.id, token.id)


def test_tokens_are_unique_and_unguessable(store: Store) -> None:
    user = add_user(store, 91, "many")
    plains = {store.create_token(user.id, f"t{i}")[1] for i in range(5)}
    assert len(plains) == 5
    assert store.find_token("scout_" + "A" * 43) is None
    assert store.find_token("not-a-token") is None


def test_users_cannot_revoke_each_others_tokens(store: Store) -> None:
    alice = add_user(store, 92, "alice")
    bob = add_user(store, 93, "bob")
    token, plain = store.create_token(alice.id, "mine")
    assert not store.revoke_token(bob.id, token.id)
    assert store.find_token(plain) is not None


def test_token_count_is_capped(store: Store) -> None:
    user = add_user(store, 94, "hoarder")
    for i in range(MAX_ACTIVE_TOKENS_PER_USER):
        store.create_token(user.id, f"t{i}")
    with pytest.raises(NotAllowed):
        store.create_token(user.id, "one too many")


# ----------------------------------------------------------------------------- cars


def test_cars_keys_and_settings(store: Store) -> None:
    car, key = store.add_car("scout-1", "Scout")
    assert key.startswith("scoutcar_") and car.availability == "admins"
    assert store.car_id_for_key(key) == "scout-1"
    assert store.car_id_for_key(key + "x") is None
    with pytest.raises(NotAllowed):
        store.add_car("scout-1", "Again")

    new_key = store.rotate_car_key("scout-1")
    assert store.car_id_for_key(key) is None
    assert store.car_id_for_key(new_key) == "scout-1"

    settings = car.settings
    settings.obst_cm = 25
    settings.name = "Rover"
    store.update_settings("scout-1", settings)
    updated = store.get_car("scout-1")
    assert updated is not None and updated.settings.obst_cm == 25 and updated.name == "Rover"

    store.add_car("scout-2", "Second")
    default = store.default_car()
    assert default is not None and default.car_id == "scout-1"


def test_bootstrap_car_key_is_applied(store: Store) -> None:
    key = "fake-bootstrap-car-key-0123456789"
    store.ensure_car_key("scout-1", key)
    assert store.car_id_for_key(key) == "scout-1"
    store.rotate_car_key("scout-1")
    store.ensure_car_key("scout-1", key)
    assert store.car_id_for_key(key) == "scout-1"
    assert len(store.list_cars()) == 1


# ------------------------------------------------------------------------- activity


def test_activity_log_and_filter(store: Store) -> None:
    alice = add_user(store, 95, "alice")
    bob = add_user(store, 96, "bob")
    store.log_activity(user_id=alice.id, car_id="scout-1", action="tool.move", detail="{}", outcome="ok")
    store.log_activity(user_id=bob.id, car_id="scout-1", action="tool.look", outcome="error: offline")
    entries = store.recent_activity()
    assert [e.action for e in entries[:2]] == ["tool.look", "tool.move"]
    only_alice = store.recent_activity(user_id=alice.id)
    assert {e.login for e in only_alice} == {"alice"}


# ------------------------------------------------------------------------ migrations


def test_migrations_are_versioned_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "scout.db"
    first = Store(path)
    assert first.schema_version() == len(MIGRATIONS)
    first.close()
    second = Store(path)
    assert second.schema_version() == len(MIGRATIONS)
    mode = second._one("PRAGMA journal_mode")
    assert mode is not None and mode[0] == "wal"
    second.close()


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "scout.db"
    Store(path).close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    raw.close()
    with pytest.raises(RuntimeError):
        Store(path)


# --------------------------------------------------------------------------- config


def _env(**values: str) -> dict[str, str]:
    base = {"SCOUT_PUBLIC_URL": "https://scout.example", "SCOUT_SECRET_KEY": FAKE_SECRET_KEY}
    base.update(values)
    return base


def test_config_from_env(tmp_path: Path) -> None:
    config = Config.from_env(
        _env(
            SCOUT_DATA_DIR=str(tmp_path),
            PORT="9000",
            SCOUT_OWNER_GITHUB_ID="1234",
            GITHUB_CLIENT_ID="fake-id",
            GITHUB_CLIENT_SECRET="fake-secret",
            SCOUT_BOOTSTRAP_TOKEN="fake-bootstrap-token-0123456789-abcdef",
        )
    )
    assert config.port == 9000 and config.owner_github_id == 1234
    assert config.github_configured and not config.dev
    assert config.public_host == "scout.example"
    assert config.car_socket_url == "wss://scout.example/car/ws"
    text = repr(config)
    assert FAKE_SECRET_KEY not in text and "fake-secret" not in text and "bootstrap" not in text


def test_config_defaults_and_railway_fallbacks() -> None:
    config = Config.from_env(
        {
            "SCOUT_SECRET_KEY": FAKE_SECRET_KEY,
            "RAILWAY_PUBLIC_DOMAIN": "scout-production.up.railway.app",
            "RAILWAY_VOLUME_MOUNT_PATH": "/data",
        }
    )
    assert config.public_url == "https://scout-production.up.railway.app"
    assert config.data_dir == Path("/data")
    assert not config.github_configured


def test_dev_config_needs_nothing() -> None:
    config = Config.from_env({"SCOUT_DEV": "1"})
    assert config.dev and config.public_url == "http://127.0.0.1:8000"
    assert len(config.secret_key) >= 32
    assert config.data_dir == Path("./data")


@pytest.mark.parametrize(
    "env",
    [
        {"SCOUT_PUBLIC_URL": "https://scout.example"},  # no secret
        _env(SCOUT_SECRET_KEY="short"),
        _env(SCOUT_PUBLIC_URL="http://scout.example"),  # http outside dev
        _env(SCOUT_PUBLIC_URL="https://scout.example/some/path"),
        _env(SCOUT_OWNER_GITHUB_ID="octocat"),  # a login, not the numeric id
        _env(SCOUT_BOOTSTRAP_TOKEN="fake-bootstrap-token-0123456789-abcdef"),  # needs an owner
        _env(SCOUT_OWNER_GITHUB_ID="1", SCOUT_BOOTSTRAP_TOKEN="too-short"),
        _env(SCOUT_BOOTSTRAP_CAR_KEY="short"),
        _env(PORT="eighty"),
        {"SCOUT_SECRET_KEY": FAKE_SECRET_KEY},  # no public URL
    ],
)
def test_bad_config_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Config.from_env(env)
