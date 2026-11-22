"""Sign in with GitHub (OAuth app web flow, no scopes, with ``state`` and PKCE).

Only the public identity is used: the numeric id, login, name and avatar. The access
token is thrown away straight after reading the profile and is never stored or logged.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
STATE_TTL_S = 600


class GitHubError(Exception):
    """Sign-in didn't work. The message is safe to show to the person signing in."""


@dataclass(frozen=True)
class GitHubIdentity:
    github_id: int
    login: str
    name: str | None
    avatar_url: str | None


@dataclass(frozen=True)
class AuthStart:
    url: str
    state: str
    verifier: str


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class GitHubOAuth:
    def __init__(
        self, client_id: str, client_secret: str, redirect_uri: str, http: httpx.AsyncClient
    ) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._http = http

    def start(self) -> AuthStart:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "allow_signup": "true",
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        return AuthStart(url=f"{AUTHORIZE_URL}?{query}", state=state, verifier=verifier)

    async def finish(self, code: str, verifier: str) -> GitHubIdentity:
        try:
            token_response = await self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": verifier,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError:
            raise GitHubError("GitHub didn't answer. Please try again.") from None
        try:
            token_body = token_response.json()
        except ValueError:
            token_body = {}
        access_token = token_body.get("access_token") if isinstance(token_body, dict) else None
        if token_response.status_code != 200 or not isinstance(access_token, str) or not access_token:
            raise GitHubError("GitHub didn't accept the sign-in. Please try again.")

        try:
            user_response = await self._http.get(
                USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except httpx.HTTPError:
            raise GitHubError("GitHub didn't answer. Please try again.") from None
        try:
            profile = user_response.json()
        except ValueError:
            profile = None
        if user_response.status_code != 200 or not isinstance(profile, dict):
            raise GitHubError("Couldn't read your GitHub profile. Please try again.")
        github_id = profile.get("id")
        login = profile.get("login")
        if not isinstance(github_id, int) or isinstance(github_id, bool) or github_id <= 0:
            raise GitHubError("Couldn't read your GitHub profile. Please try again.")
        if not isinstance(login, str) or not login:
            raise GitHubError("Couldn't read your GitHub profile. Please try again.")
        name = profile.get("name")
        avatar = profile.get("avatar_url")
        return GitHubIdentity(
            github_id=github_id,
            login=login[:100],
            name=name[:200] if isinstance(name, str) and name else None,
            avatar_url=avatar if isinstance(avatar, str) and avatar.startswith("https://") else None,
        )
