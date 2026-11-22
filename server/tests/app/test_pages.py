"""Pages, admin actions, image links, capture downloads and response headers."""

from __future__ import annotations

import html
import io
import re
import time
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scout.captures import CaptureLinks
from scout.context import Scout
from scout.images import ImageStore

from .conftest import SignIn
from .fakes import FAKE_SECRET_KEY, JPEG, OWNER_GITHUB_ID, FakeRobot, add_user, write_capture


def text_of(response_text: str) -> str:
    return html.unescape(response_text)


# ------------------------------------------------------------------- public pages


def test_home_shows_car_status_but_never_the_camera(
    client: TestClient, scout: Scout, robot: FakeRobot
) -> None:
    assert "No car has been set up yet" in client.get("/").text
    scout.store.add_car("scout-1", "Scout")
    page = client.get("/")
    assert "Scout is <strong>online</strong>" in page.text
    assert "<img" not in page.text and "/i/" not in page.text
    assert 'href="/login"' in page.text
    robot.online = False
    assert "<strong>offline</strong>" in client.get("/").text
    assert robot.names() == []  # the home page never asks the car for anything


def test_install_page_has_placeholders_and_every_client(client: TestClient) -> None:
    page = text_of(client.get("/install").text)
    header = '--header "Authorization: Bearer <your-token>"'
    assert f"claude mcp add --transport http scout http://testserver/mcp {header}" in page
    assert "codex mcp add scout --url http://testserver/mcp --bearer-token-env-var SCOUT_TOKEN" in page
    assert "Add a custom MCP server called scout at http://testserver/mcp/<your-token>" in page
    assert "Add it" in page and "@scout" in page
    assert f"grok mcp add --transport http scout http://testserver/mcp {header}" in page


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_security_headers_everywhere(client: TestClient) -> None:
    for path in ("/", "/install", "/healthz", "/static/style.css", "/no-such-page"):
        headers = client.get(path).headers
        assert "default-src 'none'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"


def test_not_found_page(client: TestClient) -> None:
    response = client.get("/no-such-page")
    assert response.status_code == 404 and "Not found" in response.text


# ------------------------------------------------------------------------ account


def test_new_token_is_shown_once_with_real_commands(
    client: TestClient, sign_in: SignIn, scout: Scout
) -> None:
    csrf = sign_in(client, 400, "maker")
    response = client.post("/account/tokens", data={"label": "Laptop", "csrf": csrf})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    page = text_of(response.text)
    match = re.search(r"(scout_[A-Za-z0-9_-]{43})", page)
    assert match
    token = match.group(1)
    assert f'--header "Authorization: Bearer {token}"' in page
    assert f'export SCOUT_TOKEN="{token}"' in page
    assert f"Add a custom MCP server called scout at http://testserver/mcp/{token}" in page
    assert token not in client.get("/account").text
    assert token[:12] in client.get("/account").text  # the prefix is listed
    user = scout.store.get_user_by_github_id(400)
    assert user is not None
    assert scout.store.find_token(token) is not None


def test_revoke_token_from_the_account_page(client: TestClient, sign_in: SignIn, scout: Scout) -> None:
    csrf = sign_in(client, 401, "revoker")
    user = scout.store.get_user_by_github_id(401)
    assert user is not None
    token, plain = scout.store.create_token(user.id, "old")
    response = client.post(f"/account/tokens/{token.id}/revoke", data={"csrf": csrf})
    assert response.status_code == 200 and "Token revoked" in response.text
    assert scout.store.find_token(plain) is None


def test_suspended_users_cannot_make_tokens(client: TestClient, sign_in: SignIn, scout: Scout) -> None:
    csrf = sign_in(client, 402, "bad")
    owner = scout.store.ensure_owner()
    user = scout.store.get_user_by_github_id(402)
    assert owner is not None and user is not None
    scout.store.set_status(user.id, "suspended", by=owner)
    response = client.post("/account/tokens", data={"label": "x", "csrf": csrf})
    assert "suspended" in response.text
    assert scout.store.list_tokens(user.id) == []


def test_account_lists_my_captures_only(
    client: TestClient, sign_in: SignIn, scout: Scout, tmp_path: Path
) -> None:
    sign_in(client, 403, "photographer")
    me = scout.store.get_user_by_github_id(403)
    assert me is not None
    write_capture(tmp_path, "-mine-0001", me.id, photos=3)
    write_capture(tmp_path, "theirs-0001", me.id + 99)
    page = client.get("/account").text
    assert "/captures/-mine-0001.zip" in page and "theirs-0001" not in page


# -------------------------------------------------------------------------- admin


def test_admin_pages_need_an_admin(client: TestClient, sign_in: SignIn) -> None:
    assert client.get("/admin").status_code == 401
    sign_in(client, 500, "regular")
    for path in ("/admin", "/admin/activity", "/admin/cars/scout-1"):
        assert client.get(path).status_code == 403


def test_admin_approves_and_manages_users(client: TestClient, sign_in: SignIn, scout: Scout) -> None:
    newbie, _ = scout.store.sign_in_github_user(github_id=600, login="newbie", name=None, avatar_url=None)
    csrf = sign_in(client, OWNER_GITHUB_ID, "owner")
    page = client.get("/admin").text
    assert "newbie" in page and "Waiting" in page

    assert client.post(f"/admin/users/{newbie.id}/approve", data={"csrf": csrf}).status_code == 200
    user = scout.store.get_user(newbie.id)
    assert user is not None and user.active and not user.is_admin

    client.post(f"/admin/users/{newbie.id}/make-admin", data={"csrf": csrf})
    user = scout.store.get_user(newbie.id)
    assert user is not None and user.admin
    client.post(f"/admin/users/{newbie.id}/remove-admin", data={"csrf": csrf})
    client.post(f"/admin/users/{newbie.id}/suspend", data={"csrf": csrf})
    user = scout.store.get_user(newbie.id)
    assert user is not None and user.status == "suspended" and not user.is_admin

    actions = [e.action for e in scout.store.recent_activity(limit=4)]
    assert actions == [
        "admin.user.suspend",
        "admin.user.remove-admin",
        "admin.user.make-admin",
        "admin.user.approve",
    ]


def test_owner_is_protected_on_the_admin_page(client: TestClient, sign_in: SignIn, scout: Scout) -> None:
    add_user(scout.store, 700, "second-admin", admin=True)
    owner = scout.store.ensure_owner()
    assert owner is not None
    csrf = sign_in(client, 700, "second-admin")
    response = client.post(f"/admin/users/{owner.id}/suspend", data={"csrf": csrf})
    assert "can't be suspended" in text_of(response.text)
    response = client.post(f"/admin/users/{owner.id}/remove-admin", data={"csrf": csrf})
    assert "always an admin" in response.text
    owner_now = scout.store.get_user(owner.id)
    assert owner_now is not None and owner_now.admin
    assert scout.store.recent_activity(limit=1)[0].outcome.startswith("refused")


def test_add_car_shows_the_key_once(client: TestClient, sign_in: SignIn, scout: Scout) -> None:
    csrf = sign_in(client, OWNER_GITHUB_ID, "owner")
    response = client.post("/admin/cars", data={"csrf": csrf, "car_id": "scout-1", "name": "Scout"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    match = re.search(r"(scoutcar_[A-Za-z0-9_-]{43})", response.text)
    assert match
    key = match.group(1)
    assert scout.store.car_id_for_key(key) == "scout-1"
    assert "ws://testserver/car/ws" in response.text
    assert f"uv run scout-sim --url ws://testserver/car/ws --key {key}" in response.text
    assert key not in client.get("/admin/cars/scout-1").text
    bad = client.post("/admin/cars", data={"csrf": csrf, "car_id": "Bad Id!", "name": "x"})
    assert "lowercase" in bad.text


def test_rotate_key(client: TestClient, sign_in: SignIn, scout: Scout, robot: FakeRobot) -> None:
    _, old_key = scout.store.add_car("scout-1", "Scout")
    csrf = sign_in(client, OWNER_GITHUB_ID, "owner")
    response = client.post("/admin/cars/scout-1/rotate-key", data={"csrf": csrf})
    match = re.search(r"(scoutcar_[A-Za-z0-9_-]{43})", response.text)
    assert match and scout.store.car_id_for_key(match.group(1)) == "scout-1"
    assert scout.store.car_id_for_key(old_key) is None
    assert "disconnect" in robot.names()


def test_car_settings_availability_and_actions(
    client: TestClient, sign_in: SignIn, scout: Scout, robot: FakeRobot
) -> None:
    scout.store.add_car("scout-1", "Scout")
    csrf = sign_in(client, OWNER_GITHUB_ID, "owner")
    page = client.get("/admin/cars/scout-1").text
    assert "OV3660" in page and "fake-wifi" in page and "0.1.0" in page

    settings = {
        "csrf": csrf, "name": "Rover", "obst_cm": "20", "min_pwm": "100", "link_ms": "1200", "vflip": "on",
        "turn_deg_per_s": "200.5", "forward_cm_per_s": "28", "strafe_cm_per_s": "18",
    }  # fmt: skip
    response = client.post("/admin/cars/scout-1/settings", data=settings)
    assert "sent to the car" in response.text
    car = scout.store.get_car("scout-1")
    assert car is not None
    assert (
        car.name == "Rover" and car.settings.obst_cm == 20 and car.settings.vflip and not car.settings.hmirror
    )
    assert car.settings.turn_deg_per_s == 200.5
    assert robot.calls[-1] == ("push_settings", {"car_id": "scout-1"})

    bad = client.post("/admin/cars/scout-1/settings", data={**settings, "obst_cm": "500"})
    assert "between 5 and 100" in bad.text

    robot.online = False
    offline = client.post("/admin/cars/scout-1/settings", data=settings)
    assert "next time it connects" in offline.text
    robot.online = True

    client.post("/admin/cars/scout-1/availability", data={"csrf": csrf, "availability": "everyone"})
    car = scout.store.get_car("scout-1")
    assert car is not None and car.availability == "everyone"

    client.post("/admin/cars/scout-1/stop", data={"csrf": csrf})
    client.post("/admin/cars/scout-1/disconnect", data={"csrf": csrf})
    sha = "ab" * 32
    client.post(
        "/admin/cars/scout-1/firmware",
        data={"csrf": csrf, "url": "https://example.com/fw.bin", "sha256": sha, "size": "123456"},
    )
    client.post(
        "/admin/cars/scout-1/wheel-test",
        data={"csrf": csrf, "bits": "163", "pwm5": "120", "pwm6": "120", "ms": "500"},
    )
    names = robot.names()
    assert {"stop_everything", "disconnect", "update_firmware", "wheel_test"} <= set(names)
    assert robot.calls[names.index("update_firmware")][1] == {
        "car_id": "scout-1", "url": "https://example.com/fw.bin", "sha256": sha, "size": 123456,
    }  # fmt: skip
    refused = client.post(
        "/admin/cars/scout-1/firmware",
        data={"csrf": csrf, "url": "http://example.com/fw.bin", "sha256": sha, "size": "1"},
    )
    assert "https://" in refused.text
    logged = {e.action for e in scout.store.recent_activity()}
    assert {"admin.car.settings", "admin.car.availability", "admin.car.stop", "admin.car.firmware"} <= logged


def test_admin_releases_the_driver(client: TestClient, sign_in: SignIn, scout: Scout) -> None:
    scout.store.add_car("scout-1", "Scout")
    alice = add_user(scout.store, 800, "alice")
    scout.lock.acquire("scout-1", alice.id, alice.login)
    csrf = sign_in(client, OWNER_GITHUB_ID, "owner")
    assert "alice" in client.get("/admin/cars/scout-1").text
    response = client.post("/admin/cars/scout-1/release", data={"csrf": csrf})
    assert "alice is no longer driving" in response.text
    assert scout.lock.current("scout-1") is None


def test_activity_log_filter(client: TestClient, sign_in: SignIn, scout: Scout) -> None:
    alice = add_user(scout.store, 900, "alice")
    bob = add_user(scout.store, 901, "bob")
    scout.store.log_activity(user_id=alice.id, car_id="scout-1", action="tool.move", detail='{"ms": 500}')
    scout.store.log_activity(user_id=bob.id, car_id="scout-1", action="tool.look")
    sign_in(client, OWNER_GITHUB_ID, "owner")
    everyone = client.get("/admin/activity").text
    assert "tool.move" in everyone and "tool.look" in everyone
    only_alice = client.get(f"/admin/activity?user={alice.id}").text
    assert "tool.move" in only_alice and "tool.look" not in only_alice


# -------------------------------------------------------------------- image links


def test_image_links_expire() -> None:
    now = [0.0]
    store = ImageStore(ttl=900, clock=lambda: now[0])
    image_id = store.put(JPEG)
    assert image_id is not None and len(image_id) >= 32
    assert store.get(image_id) == JPEG
    now[0] = 899
    assert store.get(image_id) == JPEG
    now[0] = 900
    assert store.get(image_id) is None
    assert len(store) == 0 and store.total_bytes == 0


def test_image_store_is_capped() -> None:
    store = ImageStore(max_bytes=100, max_items=3, max_image_bytes=60)
    assert store.put(b"x" * 61) is None
    ids = [store.put(b"y" * 40) for _ in range(3)]
    assert store.get(ids[0] or "") is None  # pushed out by size
    assert len(store) == 2 and store.total_bytes == 80
    for _ in range(5):
        store.put(b"z")
    assert len(store) <= 3


def test_image_route(client: TestClient, scout: Scout) -> None:
    image_id = scout.images.put(JPEG)
    response = client.get(f"/i/{image_id}")
    assert response.status_code == 200 and response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert client.get("/i/not-a-real-image-id-1234567890").status_code == 404
    assert client.get("/i/..%2F..%2Fscout.db").status_code == 404


# -------------------------------------------------------------- capture downloads


def _zip_names(content: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.testzip() is None
        return sorted(archive.namelist())


def test_capture_download_rules(client: TestClient, sign_in: SignIn, scout: Scout, tmp_path: Path) -> None:
    owner_id = add_user(scout.store, 1100, "maker").id
    write_capture(tmp_path, "cap-1", owner_id, photos=2)
    url = "/captures/cap-1.zip"

    assert client.get(url).status_code == 401  # signed out

    sign_in(client, 1101, "stranger")
    assert client.get(url).status_code == 404  # someone else's

    sign_in(client, 1100, "maker")
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="scout-cap-1.zip"'
    assert _zip_names(response.content) == [
        "scout-cap-1/001.jpg",
        "scout-cap-1/002.jpg",
        "scout-cap-1/meta.json",
    ]

    sign_in(client, OWNER_GITHUB_ID, "owner")
    assert client.get(url).status_code == 200  # admins may

    assert client.get("/captures/missing.zip").status_code == 404
    assert client.get("/captures/..%2Fscout.db.zip").status_code == 404


def test_signed_capture_links(client: TestClient, scout: Scout, tmp_path: Path) -> None:
    write_capture(tmp_path, "cap-2", 12345)
    link = scout.capture_links.link("cap-2").removeprefix("http://testserver")
    client.cookies.clear()
    response = client.get(link)
    assert response.status_code == 200 and response.content[:2] == b"PK"

    tampered = re.sub(r"sig=[^&]+", "sig=AAAA", link)
    assert client.get(tampered).status_code == 401
    other = link.replace("cap-2", "cap-3")
    write_capture(tmp_path, "cap-3", 12345)
    assert client.get(other).status_code == 401  # a signature only fits its own capture


def test_capture_links_expire() -> None:
    links = CaptureLinks("http://testserver", FAKE_SECRET_KEY, ttl=60)
    link = links.link("cap-9", now=1000)
    exp = re.search(r"exp=(\d+)", link)
    sig = re.search(r"sig=([^&]+)", link)
    assert exp and sig
    assert links.verify("cap-9", exp.group(1), sig.group(1), now=1059)
    assert not links.verify("cap-9", exp.group(1), sig.group(1), now=1061)
    assert not links.verify("cap-9", str(int(exp.group(1)) + 3600), sig.group(1), now=1000)
    other_key = CaptureLinks("http://testserver", "another-fake-secret-key-0123456789abcdef", ttl=60)
    assert not other_key.verify("cap-9", exp.group(1), sig.group(1), now=1000)


@pytest.mark.parametrize("photos", [0, 40])
def test_zip_streams_any_size(tmp_path: Path, photos: int) -> None:
    from scout.captures import iter_zip

    folder = write_capture(tmp_path, "big", 1, photos=photos)
    (folder / "large.bin").write_bytes(b"\x00" * (700 * 1024))
    chunks = list(iter_zip(folder, "scout-big"))
    assert len(chunks) > 1
    names = _zip_names(b"".join(chunks))
    assert "scout-big/large.bin" in names and len(names) == photos + 2


def test_captures_sorted_newest_first(tmp_path: Path) -> None:
    from scout.captures import list_user_captures

    write_capture(tmp_path, "older", 5)
    write_capture(tmp_path, "newer", 5)
    meta = tmp_path / "captures" / "newer" / "meta.json"
    meta.write_text(meta.read_text().replace("2026-11-20T10:00:00+00:00", "2026-11-21T10:00:00+00:00"))
    found = list_user_captures(tmp_path / "captures", 5)
    assert [c.capture_id for c in found] == ["newer", "older"]
    assert found[0].created is not None and found[0].created < time.time() + 10**9
