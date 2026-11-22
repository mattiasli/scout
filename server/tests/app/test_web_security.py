"""Website security: GitHub sign-in (mocked), CSRF, cookies and sign-out."""

from __future__ import annotations

import html
from collections.abc import Callable
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from scout.context import Scout

from .conftest import SignIn
from .fakes import OWNER_GITHUB_ID, FakeGitHub, authorize_params, csrf_from


def test_login_redirects_to_github_with_state_and_pkce_and_no_scopes(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    params = authorize_params(location)
    assert params["client_id"] == "fake-client-id"
    assert params["redirect_uri"] == "http://testserver/auth/github/callback"
    assert len(params["state"]) >= 32
    assert params["code_challenge_method"] == "S256" and len(params["code_challenge"]) >= 43
    assert "scope" not in params
    assert "fake-client-secret" not in location


def test_full_sign_in_creates_a_waiting_account(
    client: TestClient, sign_in: SignIn, scout: Scout, github: FakeGitHub
) -> None:
    sign_in(client, 4242, "octo", "Octo Cat")
    page = client.get("/account")
    assert page.status_code == 200
    assert "waiting for an admin to approve" in page.text
    user = scout.store.get_user_by_github_id(4242)
    assert user is not None and user.status == "waiting" and user.name == "Octo Cat"
    # The token exchange sent the PKCE verifier and the secret, and only to GitHub.
    exchange = github.token_requests[-1]
    assert exchange["code_verifier"] and exchange["client_secret"] == "fake-client-secret"
    assert scout.store.recent_activity(limit=1)[0].action == "account.sign_in"


def test_owner_signs_in_as_active_admin(client: TestClient, sign_in: SignIn) -> None:
    sign_in(client, OWNER_GITHUB_ID, "the-owner")
    page = client.get("/account")
    assert "Approved" in page.text and "Owner" in page.text
    assert client.get("/admin").status_code == 200


def test_callback_rejects_a_wrong_or_missing_state(client: TestClient, github: FakeGitHub) -> None:
    github.add_user("fake-code", 5, "mallory")
    client.get("/login", follow_redirects=False)
    wrong = client.get("/auth/github/callback", params={"code": "fake-code", "state": "forged"})
    assert wrong.status_code == 400
    # The state is single-use: even the right one fails after a failed attempt.
    fresh = client.get("/auth/github/callback", params={"code": "fake-code", "state": "x" * 43})
    assert fresh.status_code == 400
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_state_cannot_be_replayed(client: TestClient, github: FakeGitHub) -> None:
    github.add_user("fake-code-2", 6, "replayer")
    params = authorize_params(client.get("/login", follow_redirects=False).headers["location"])
    first = client.get(
        "/auth/github/callback",
        params={"code": "fake-code-2", "state": params["state"]},
        follow_redirects=False,
    )
    assert first.status_code == 303
    client.post("/logout", data={"csrf": csrf_from(client.get("/account").text)})
    again = client.get("/auth/github/callback", params={"code": "fake-code-2", "state": params["state"]})
    assert again.status_code == 400


def test_github_refusing_the_code_is_handled(client: TestClient, github: FakeGitHub) -> None:
    github.add_user("fake-code-3", 7, "unlucky")
    github.fail_token = True
    params = authorize_params(client.get("/login", follow_redirects=False).headers["location"])
    response = client.get("/auth/github/callback", params={"code": "fake-code-3", "state": params["state"]})
    assert response.status_code == 502
    assert "GitHub didn't accept" in html.unescape(response.text)


def test_user_cancelling_on_github(client: TestClient) -> None:
    client.get("/login", follow_redirects=False)
    response = client.get("/auth/github/callback", params={"error": "access_denied"})
    assert response.status_code == 200 and "cancelled" in response.text


def test_sign_in_not_configured(make_app: Callable[..., Starlette]) -> None:
    app = make_app(github_client_id=None, github_client_secret=None)
    with TestClient(app) as client:
        response = client.get("/login")
        assert response.status_code == 503
        assert "isn't set up yet" in html.unescape(response.text)


def test_forms_need_the_csrf_token(client: TestClient, sign_in: SignIn, scout: Scout) -> None:
    csrf = sign_in(client, 300, "formy")
    assert client.post("/account/tokens", data={"label": "x"}).status_code == 403
    assert client.post("/account/tokens", data={"label": "x", "csrf": "wrong"}).status_code == 403
    user = scout.store.get_user_by_github_id(300)
    assert user is not None and scout.store.list_tokens(user.id) == []
    ok = client.post("/account/tokens", data={"label": "x", "csrf": csrf})
    assert ok.status_code == 200 and len(scout.store.list_tokens(user.id)) == 1


def test_forms_from_another_origin_are_refused(client: TestClient, sign_in: SignIn) -> None:
    csrf = sign_in(client, 301, "origin")
    response = client.post(
        "/account/tokens", data={"label": "x", "csrf": csrf}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    same = client.post(
        "/account/tokens", data={"label": "x", "csrf": csrf}, headers={"Origin": "http://testserver"}
    )
    assert same.status_code == 200


def test_logout_is_post_with_csrf(client: TestClient, sign_in: SignIn) -> None:
    csrf = sign_in(client, 302, "leaver")
    assert client.get("/logout").status_code == 405
    assert client.post("/logout", data={"csrf": "wrong"}).status_code == 403
    assert client.get("/account", follow_redirects=False).status_code == 200
    out = client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_secure_cookie_flags_in_production(make_app: Callable[..., Starlette], github: FakeGitHub) -> None:
    app = make_app(public_url="https://scout.example", dev=False)
    with TestClient(app, base_url="https://scout.example") as client:
        response = client.get("/login", follow_redirects=False)
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-scout=")
        lowered = cookie.lower()
        assert (
            "httponly" in lowered
            and "samesite=lax" in lowered
            and "secure" in lowered
            and "path=/" in lowered
        )
        assert "strict-transport-security" in client.get("/").headers


def test_dev_cookie_is_httponly_and_lax(client: TestClient) -> None:
    cookie = client.get("/login", follow_redirects=False).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_session_is_renewed_on_sign_in(client: TestClient, sign_in: SignIn) -> None:
    client.get("/login", follow_redirects=False)
    before = dict(client.cookies)
    sign_in(client, 303, "renewed")
    assert dict(client.cookies) != before


def test_secret_key_never_appears_in_pages(client: TestClient, sign_in: SignIn, tmp_path: Path) -> None:
    sign_in(client, OWNER_GITHUB_ID, "owner")
    for path in ("/", "/install", "/account", "/admin", "/admin/activity", "/healthz"):
        body = client.get(path).text
        assert "fake-secret-key-for-tests-only" not in body
        assert "fake-client-secret" not in body
