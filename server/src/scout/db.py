"""SQLite storage: users, tokens, cars and the activity log.

One connection, opened at start-up, in WAL mode with ``synchronous=NORMAL``. Every query
here is a primary-key or indexed lookup on small tables, and a WAL commit with
``synchronous=NORMAL`` doesn't wait for an fsync, so each call takes well under a
millisecond. That is cheaper than hopping to a worker thread, so the repository is
called directly from the event loop. A lock keeps the connection safe if a test client
or a worker thread ever calls in from another thread.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from scout.contracts import CarSettings
from scout.security import hash_secret, new_car_key, new_token, same_secret, shown_prefix

log = logging.getLogger(__name__)

Status = Literal["waiting", "active", "suspended"]
Availability = Literal["admins", "everyone"]
STATUSES: tuple[Status, ...] = ("waiting", "active", "suspended")
AVAILABILITIES: tuple[Availability, ...] = ("admins", "everyone")

MAX_ACTIVE_TOKENS_PER_USER = 25
ACTIVITY_KEEP_DAYS = 180
ACTIVITY_KEEP_ROWS = 200_000
TOKEN_TOUCH_INTERVAL_S = 60.0

MIGRATIONS: list[str] = [
    # 1: the first schema.
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        github_id INTEGER NOT NULL UNIQUE,
        login TEXT NOT NULL,
        name TEXT,
        avatar_url TEXT,
        status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'active', 'suspended')),
        is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
        created_at REAL NOT NULL,
        approved_at REAL,
        approved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        last_login_at REAL
    );
    CREATE TABLE tokens (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        label TEXT NOT NULL,
        prefix TEXT NOT NULL,
        hash TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL,
        last_used_at REAL,
        revoked_at REAL
    );
    CREATE INDEX tokens_user ON tokens(user_id);
    CREATE TABLE cars (
        car_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        availability TEXT NOT NULL DEFAULT 'admins' CHECK (availability IN ('admins', 'everyone')),
        settings TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    );
    CREATE TABLE activity (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        car_id TEXT,
        action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT 'ok'
    );
    CREATE INDEX activity_ts ON activity(ts);
    CREATE INDEX activity_user_ts ON activity(user_id, ts);
    """,
]


@dataclass(frozen=True)
class User:
    id: int
    github_id: int
    login: str
    name: str | None
    avatar_url: str | None
    status: Status
    is_admin: bool
    created_at: float
    approved_at: float | None
    approved_by: int | None
    last_login_at: float | None
    is_owner: bool = False

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def admin(self) -> bool:
        """An admin who may act as one right now (an active account with the admin flag)."""
        return self.status == "active" and self.is_admin

    @property
    def display_name(self) -> str:
        return self.name or self.login


@dataclass(frozen=True)
class Token:
    id: int
    user_id: int
    label: str
    prefix: str
    created_at: float
    last_used_at: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class Car:
    car_id: str
    name: str
    key_prefix: str
    availability: Availability
    settings: CarSettings
    created_at: float


@dataclass(frozen=True)
class Activity:
    id: int
    ts: float
    user_id: int | None
    login: str | None
    car_id: str | None
    action: str
    detail: str
    outcome: str


class NotAllowed(Exception):
    """A change the rules don't allow (for example demoting the owner). The message is for people."""


_SETTINGS_FIELDS = {f.name for f in dataclasses.fields(CarSettings)} - {"name"}


def settings_from_json(name: str, text: str) -> CarSettings:
    try:
        raw = json.loads(text or "{}")
    except ValueError:
        raw = {}
    values = {k: v for k, v in raw.items() if k in _SETTINGS_FIELDS} if isinstance(raw, dict) else {}
    try:
        return CarSettings(name=name, **values)
    except TypeError:
        return CarSettings(name=name)


def settings_to_json(settings: CarSettings) -> str:
    data = dataclasses.asdict(settings)
    data.pop("name", None)
    return json.dumps(data, sort_keys=True)


class Store:
    """The repository. Every method is synchronous and fast (see the module docstring)."""

    def __init__(
        self,
        path: Path | str,
        *,
        owner_github_id: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.owner_github_id = owner_github_id
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._activity_writes = 0
        self.migrate()

    # ----------------------------------------------------------------- plumbing

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, args).fetchone()
            return row

    def _all(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args).fetchall())

    def schema_version(self) -> int:
        row = self._one("PRAGMA user_version")
        return int(row[0]) if row else 0

    def migrate(self) -> None:
        with self._lock:
            current = self.schema_version()
            if current > len(MIGRATIONS):
                raise RuntimeError(
                    f"The database is at schema version {current}, newer than this server "
                    f"({len(MIGRATIONS)}). Deploy a newer server."
                )
            for version in range(current + 1, len(MIGRATIONS) + 1):
                log.info("Migrating the database to schema version %d", version)
                script = MIGRATIONS[version - 1]
                # executescript commits first, so wrap the script in its own transaction.
                try:
                    self._conn.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
                    )
                except sqlite3.Error:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise

    def ping(self) -> bool:
        try:
            self._one("SELECT 1")
        except sqlite3.Error:
            return False
        return True

    # -------------------------------------------------------------------- users

    def _user(self, row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        github_id = int(row["github_id"])
        return User(
            id=int(row["id"]),
            github_id=github_id,
            login=row["login"],
            name=row["name"],
            avatar_url=row["avatar_url"],
            status=row["status"],
            is_admin=bool(row["is_admin"]),
            created_at=float(row["created_at"]),
            approved_at=row["approved_at"],
            approved_by=row["approved_by"],
            last_login_at=row["last_login_at"],
            is_owner=self.owner_github_id is not None and github_id == self.owner_github_id,
        )

    def get_user(self, user_id: int) -> User | None:
        return self._user(self._one("SELECT * FROM users WHERE id = ?", (user_id,)))

    def get_user_by_github_id(self, github_id: int) -> User | None:
        return self._user(self._one("SELECT * FROM users WHERE github_id = ?", (github_id,)))

    def list_users(self) -> list[User]:
        rows = self._all(
            "SELECT * FROM users ORDER BY CASE status WHEN 'waiting' THEN 0 ELSE 1 END, created_at DESC"
        )
        return [u for u in (self._user(r) for r in rows) if u is not None]

    def count_waiting(self) -> int:
        row = self._one("SELECT COUNT(*) FROM users WHERE status = 'waiting'")
        return int(row[0]) if row else 0

    def sign_in_github_user(
        self, *, github_id: int, login: str, name: str | None, avatar_url: str | None
    ) -> tuple[User, bool]:
        """Create or update the account for a GitHub identity. Returns (user, created).

        New accounts start as waiting. The owner is always active and an admin.
        """
        now = self._clock()
        is_owner = self.owner_github_id is not None and github_id == self.owner_github_id
        with self._tx() as conn:
            row = conn.execute("SELECT id FROM users WHERE github_id = ?", (github_id,)).fetchone()
            created = row is None
            if created:
                conn.execute(
                    "INSERT INTO users (github_id, login, name, avatar_url, status, is_admin, created_at,"
                    " approved_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        github_id,
                        login,
                        name,
                        avatar_url,
                        "active" if is_owner else "waiting",
                        1 if is_owner else 0,
                        now,
                        now if is_owner else None,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE users SET login = ?, name = ?, avatar_url = ?, last_login_at = ?"
                    " WHERE github_id = ?",
                    (login, name, avatar_url, now, github_id),
                )
        if is_owner:
            self.ensure_owner()
        user = self.get_user_by_github_id(github_id)
        assert user is not None
        return user, created

    def ensure_owner(self) -> User | None:
        """Make sure the owner's account exists (as a placeholder until they sign in) and is
        active and an admin. Returns it, or None when no owner is configured."""
        if self.owner_github_id is None:
            return None
        now = self._clock()
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (github_id, login, status, is_admin, created_at, approved_at)"
                " VALUES (?, 'owner', 'active', 1, ?, ?)",
                (self.owner_github_id, now, now),
            )
            conn.execute(
                "UPDATE users SET status = 'active', is_admin = 1, approved_at = COALESCE(approved_at, ?)"
                " WHERE github_id = ?",
                (now, self.owner_github_id),
            )
        return self.get_user_by_github_id(self.owner_github_id)

    def set_status(self, user_id: int, status: Status, *, by: User) -> User:
        if status not in STATUSES:
            raise ValueError(status)
        user = self.get_user(user_id)
        if user is None:
            raise NotAllowed("That account doesn't exist.")
        if user.is_owner and status != "active":
            raise NotAllowed("The owner's account can't be suspended.")
        if user.id == by.id and status != "active":
            raise NotAllowed("You can't suspend your own account. Ask another admin.")
        with self._tx() as conn:
            if status == "active" and user.status != "active":
                conn.execute(
                    "UPDATE users SET status = 'active', approved_at = ?, approved_by = ? WHERE id = ?",
                    (self._clock(), by.id, user_id),
                )
            else:
                conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        updated = self.get_user(user_id)
        assert updated is not None
        return updated

    def set_admin(self, user_id: int, is_admin: bool, *, by: User) -> User:
        user = self.get_user(user_id)
        if user is None:
            raise NotAllowed("That account doesn't exist.")
        if user.is_owner and not is_admin:
            raise NotAllowed("The owner is always an admin.")
        if user.id == by.id and not is_admin:
            raise NotAllowed("You can't remove your own admin status. Ask another admin.")
        if is_admin and user.status != "active":
            raise NotAllowed("Approve the account first, then make it an admin.")
        with self._tx() as conn:
            conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (1 if is_admin else 0, user_id))
        updated = self.get_user(user_id)
        assert updated is not None
        return updated

    # ------------------------------------------------------------------- tokens

    def _token(self, row: sqlite3.Row) -> Token:
        return Token(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            label=row["label"],
            prefix=row["prefix"],
            created_at=float(row["created_at"]),
            last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"],
        )

    def create_token(self, user_id: int, label: str) -> tuple[Token, str]:
        """Make a new token. Returns it with the plain text, which is never stored."""
        label = " ".join(label.split())[:60] or "Token"
        plain = new_token()
        now = self._clock()
        with self._tx() as conn:
            (active,) = conn.execute(
                "SELECT COUNT(*) FROM tokens WHERE user_id = ? AND revoked_at IS NULL", (user_id,)
            ).fetchone()
            if active >= MAX_ACTIVE_TOKENS_PER_USER:
                raise NotAllowed(f"You already have {active} tokens. Revoke one you don't use first.")
            cur = conn.execute(
                "INSERT INTO tokens (user_id, label, prefix, hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, label, shown_prefix(plain), hash_secret(plain), now),
            )
            token_id = int(cur.lastrowid or 0)
        row = self._one("SELECT * FROM tokens WHERE id = ?", (token_id,))
        assert row is not None
        return self._token(row), plain

    def list_tokens(self, user_id: int) -> list[Token]:
        rows = self._all(
            "SELECT * FROM tokens WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at DESC",
            (user_id,),
        )
        return [self._token(r) for r in rows]

    def revoke_token(self, user_id: int, token_id: int) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                (self._clock(), token_id, user_id),
            )
            return cur.rowcount > 0

    def find_token(self, plain: str) -> Token | None:
        """Look up an unrevoked token by its plain text (via its hash)."""
        if not plain.startswith("scout_") or len(plain) > 200:
            return None
        digest = hash_secret(plain)
        row = self._one("SELECT * FROM tokens WHERE hash = ? AND revoked_at IS NULL", (digest,))
        if row is None or not same_secret(row["hash"], digest):
            return None
        token = self._token(row)
        now = self._clock()
        if token.last_used_at is None or now - token.last_used_at >= TOKEN_TOUCH_INTERVAL_S:
            with self._tx() as conn:
                conn.execute("UPDATE tokens SET last_used_at = ? WHERE id = ?", (now, token.id))
        return token

    # --------------------------------------------------------------------- cars

    def _car(self, row: sqlite3.Row | None) -> Car | None:
        if row is None:
            return None
        return Car(
            car_id=row["car_id"],
            name=row["name"],
            key_prefix=row["key_prefix"],
            availability=row["availability"],
            settings=settings_from_json(row["name"], row["settings"]),
            created_at=float(row["created_at"]),
        )

    def list_cars(self) -> list[Car]:
        rows = self._all("SELECT * FROM cars ORDER BY created_at, car_id")
        return [c for c in (self._car(r) for r in rows) if c is not None]

    def get_car(self, car_id: str) -> Car | None:
        return self._car(self._one("SELECT * FROM cars WHERE car_id = ?", (car_id,)))

    def default_car(self) -> Car | None:
        """The car the MCP tools drive: the only one, or the first one added."""
        return self._car(self._one("SELECT * FROM cars ORDER BY created_at, car_id LIMIT 1"))

    def add_car(self, car_id: str, name: str, *, key: str | None = None) -> tuple[Car, str]:
        """Add a car. Returns it with its key in plain text, which is never stored."""
        plain = key or new_car_key()
        settings = CarSettings(name=name)
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO cars (car_id, name, key_hash, key_prefix, settings, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        car_id,
                        name,
                        hash_secret(plain),
                        shown_prefix(plain),
                        settings_to_json(settings),
                        self._clock(),
                    ),
                )
        except sqlite3.IntegrityError:
            raise NotAllowed(f"A car called {car_id} already exists.") from None
        car = self.get_car(car_id)
        assert car is not None
        return car, plain

    def rotate_car_key(self, car_id: str, *, key: str | None = None) -> str:
        plain = key or new_car_key()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE cars SET key_hash = ?, key_prefix = ? WHERE car_id = ?",
                (hash_secret(plain), shown_prefix(plain), car_id),
            )
            if cur.rowcount == 0:
                raise NotAllowed("That car doesn't exist.")
        return plain

    def ensure_car_key(self, car_id: str, key: str, *, name: str = "Scout") -> None:
        """Make sure ``car_id`` exists and accepts ``key`` (used for SCOUT_BOOTSTRAP_CAR_KEY)."""
        car_row = self._one("SELECT key_hash FROM cars WHERE car_id = ?", (car_id,))
        if car_row is None:
            self.add_car(car_id, name, key=key)
            log.info("Added car %s from SCOUT_BOOTSTRAP_CAR_KEY", car_id)
        elif not same_secret(car_row["key_hash"], hash_secret(key)):
            self.rotate_car_key(car_id, key=key)
            log.info("Set the key of car %s from SCOUT_BOOTSTRAP_CAR_KEY", car_id)

    def car_id_for_key(self, key: str) -> str | None:
        if not key or len(key) > 200:
            return None
        digest = hash_secret(key)
        row = self._one("SELECT car_id, key_hash FROM cars WHERE key_hash = ?", (digest,))
        if row is None or not same_secret(row["key_hash"], digest):
            return None
        return str(row["car_id"])

    def set_availability(self, car_id: str, availability: Availability) -> None:
        if availability not in AVAILABILITIES:
            raise ValueError(availability)
        with self._tx() as conn:
            conn.execute("UPDATE cars SET availability = ? WHERE car_id = ?", (availability, car_id))

    def update_settings(self, car_id: str, settings: CarSettings) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE cars SET name = ?, settings = ? WHERE car_id = ?",
                (settings.name, settings_to_json(settings), car_id),
            )

    # ----------------------------------------------------------------- activity

    def log_activity(
        self,
        *,
        user_id: int | None,
        car_id: str | None,
        action: str,
        detail: str = "",
        outcome: str = "ok",
    ) -> None:
        now = self._clock()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO activity (ts, user_id, car_id, action, detail, outcome)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (now, user_id, car_id, action[:60], detail[:500], outcome[:300]),
            )
        self._activity_writes += 1
        if self._activity_writes % 1000 == 1:
            self.prune_activity()

    def prune_activity(self) -> None:
        cutoff = self._clock() - ACTIVITY_KEEP_DAYS * 86400
        with self._tx() as conn:
            conn.execute("DELETE FROM activity WHERE ts < ?", (cutoff,))
            conn.execute(
                "DELETE FROM activity WHERE id <="
                " (SELECT id FROM activity ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (ACTIVITY_KEEP_ROWS,),
            )

    def recent_activity(self, *, limit: int = 200, user_id: int | None = None) -> list[Activity]:
        sql = (
            "SELECT a.*, u.login AS login FROM activity a LEFT JOIN users u ON u.id = a.user_id"
            + (" WHERE a.user_id = ?" if user_id is not None else "")
            + " ORDER BY a.id DESC LIMIT ?"
        )
        args: tuple[Any, ...] = (user_id, limit) if user_id is not None else (limit,)
        return [
            Activity(
                id=int(r["id"]),
                ts=float(r["ts"]),
                user_id=r["user_id"],
                login=r["login"],
                car_id=r["car_id"],
                action=r["action"],
                detail=r["detail"],
                outcome=r["outcome"],
            )
            for r in self._all(sql, args)
        ]
