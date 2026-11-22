"""The website: public pages, GitHub sign-in, the account page, admin pages and downloads."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import anyio
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from scout import __version__
from scout.captures import iter_zip, list_user_captures, read_meta, valid_capture_id
from scout.context import Scout
from scout.contracts import CarBusy, CarError, CarOffline, CarSettings
from scout.db import AVAILABILITIES, NotAllowed, User
from scout.github import STATE_TTL_S, GitHubError
from scout.security import new_csrf_token, same_secret

log = logging.getLogger(__name__)

CAR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_FIRMWARE_BYTES = 16 * 1024 * 1024
CAR_ERRORS = (CarOffline, CarBusy, CarError)

Handler = Callable[[Request], Awaitable[Response]]


# --------------------------------------------------------------------------- helpers


def scout_of(request: Request) -> Scout:
    scout: Scout = request.app.state.scout
    return scout


def current_user(request: Request) -> User | None:
    uid = request.session.get("uid")
    if not isinstance(uid, int):
        return None
    user = scout_of(request).store.get_user(uid)
    if user is None:
        request.session.clear()
    return user


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str):
        token = new_csrf_token()
        request.session["csrf"] = token
    return token


def flash(request: Request, message: str, kind: str = "ok") -> None:
    messages = list(request.session.get("flash", []))[-4:]
    messages.append({"kind": kind, "text": message[:500]})
    request.session["flash"] = messages


def render(
    request: Request,
    template: str,
    context: Mapping[str, Any] | None = None,
    *,
    status_code: int = 200,
    user: User | None = None,
) -> Response:
    scout = scout_of(request)
    user = user if user is not None else current_user(request)
    messages = request.session.pop("flash", []) if "flash" in request.session else []
    full: dict[str, Any] = {
        "user": user,
        "csrf": csrf_token(request) if user is not None else "",
        "messages": messages,
        "waiting_count": scout.store.count_waiting() if user is not None and user.admin else 0,
        "page": template.removesuffix(".html"),
    }
    full.update(context or {})
    return scout.templates.TemplateResponse(
        request, template, full, status_code=status_code, headers={"Cache-Control": "no-store"}
    )


def message_page(request: Request, title: str, text: str, *, status_code: int = 200) -> Response:
    return render(request, "message.html", {"title": title, "text": text}, status_code=status_code)


def see_other(path: str) -> Response:
    return RedirectResponse(path, status_code=303)


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        return True  # the CSRF token is the real check; Origin is a second line of defence
    config = scout_of(request).config
    if origin == config.public_url:
        return True
    if config.dev:
        host = request.headers.get("host", "")
        return origin in {f"http://{host}", f"https://{host}"}
    return False


async def checked_form(request: Request) -> FormData:
    """The posted form, after checking the CSRF token and the Origin header."""
    form = await request.form()
    sent = form.get("csrf")
    expected = request.session.get("csrf")
    if (
        not isinstance(sent, str)
        or not isinstance(expected, str)
        or not same_secret(sent, expected)
        or not _origin_ok(request)
    ):
        raise HTTPException(403, "This form has expired. Go back, reload the page and try again.")
    return form


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Please sign in first.")
    return user


def require_admin(request: Request) -> User:
    user = require_user(request)
    if not user.admin:
        raise HTTPException(403, "Only admins can open this page.")
    return user


def form_text(form: FormData, name: str, default: str = "") -> str:
    value = form.get(name, default)
    return value.strip() if isinstance(value, str) else default


def log_admin(
    request: Request, admin: User, action: str, detail: str, *, car_id: str | None = None, outcome: str = "ok"
) -> None:
    scout_of(request).store.log_activity(
        user_id=admin.id, car_id=car_id, action=f"admin.{action}", detail=detail, outcome=outcome
    )


# ---------------------------------------------------------------------- public pages


async def home(request: Request) -> Response:
    scout = scout_of(request)
    car = scout.store.default_car()
    summary = scout.robot.car(car.car_id) if car is not None else None
    return render(
        request,
        "home.html",
        {"car": car, "online": bool(summary and summary.online)},
    )


async def install(request: Request) -> Response:
    return render(request, "install.html", {"token": None})


async def healthz(request: Request) -> Response:
    ok = scout_of(request).store.ping()
    return JSONResponse(
        {"ok": ok, "version": __version__},
        status_code=200 if ok else 503,
        headers={"Cache-Control": "no-store"},
    )


async def image(request: Request) -> Response:
    data = scout_of(request).images.get(request.path_params["image_id"])
    if data is None:
        return Response("This picture link has expired.", status_code=404, media_type="text/plain")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=900"})


# --------------------------------------------------------------------------- sign-in


async def login(request: Request) -> Response:
    scout = scout_of(request)
    if scout.github is None:
        return message_page(
            request,
            "Sign-in isn't set up yet",
            "The owner of this Scout server hasn't connected GitHub sign-in yet. Please check back later.",
            status_code=503,
        )
    if current_user(request) is not None:
        return see_other("/account")
    start = scout.github.start()
    request.session["oauth"] = {"state": start.state, "verifier": start.verifier, "ts": time.time()}
    return RedirectResponse(start.url, status_code=302)


async def github_callback(request: Request) -> Response:
    scout = scout_of(request)
    if scout.github is None:
        return message_page(
            request, "Sign-in isn't set up yet", "GitHub sign-in isn't configured.", status_code=503
        )
    pending = request.session.pop("oauth", None)
    params = request.query_params
    if params.get("error"):
        return message_page(
            request, "Sign-in cancelled", "GitHub didn't sign you in. You can try again any time."
        )
    state, code = params.get("state"), params.get("code")
    if (
        not isinstance(pending, dict)
        or not state
        or not code
        or not isinstance(pending.get("state"), str)
        or not same_secret(state, pending["state"])
        or not isinstance(pending.get("ts"), int | float)
        or time.time() - pending["ts"] > STATE_TTL_S
    ):
        return message_page(
            request,
            "Sign-in expired",
            "That sign-in link has expired or was already used. Please sign in again.",
            status_code=400,
        )
    try:
        identity = await scout.github.finish(code, str(pending.get("verifier", "")))
    except GitHubError as exc:
        return message_page(request, "Sign-in didn't work", str(exc), status_code=502)

    user, created = scout.store.sign_in_github_user(
        github_id=identity.github_id, login=identity.login, name=identity.name, avatar_url=identity.avatar_url
    )
    scout.store.log_activity(
        user_id=user.id, car_id=None, action="account.sign_in", detail="new account" if created else ""
    )
    request.session.clear()  # a fresh session on every sign-in
    request.session["uid"] = user.id
    request.session["csrf"] = new_csrf_token()
    return see_other("/account")


async def logout(request: Request) -> Response:
    await checked_form(request)
    request.session.clear()
    return see_other("/")


# --------------------------------------------------------------------------- account


async def _account_page(
    request: Request, user: User, *, new_token: str | None = None, new_token_label: str | None = None
) -> Response:
    scout = scout_of(request)
    captures = await anyio.to_thread.run_sync(list_user_captures, scout.config.captures_dir, user.id)
    return render(
        request,
        "account.html",
        {
            "tokens": scout.store.list_tokens(user.id),
            "captures": captures,
            "new_token": new_token,
            "new_token_label": new_token_label,
        },
        user=user,
    )


async def account(request: Request) -> Response:
    user = current_user(request)
    if user is None:
        return see_other("/login")
    return await _account_page(request, user)


async def create_token(request: Request) -> Response:
    user = require_user(request)
    form = await checked_form(request)
    scout = scout_of(request)
    if user.status == "suspended":
        flash(request, "Your account is suspended, so you can't make new tokens.", "error")
        return see_other("/account")
    label = form_text(form, "label") or "My token"
    try:
        token, plain = scout.store.create_token(user.id, label)
    except NotAllowed as exc:
        flash(request, str(exc), "error")
        return see_other("/account")
    scout.store.log_activity(
        user_id=user.id, car_id=None, action="token.create", detail=f"{token.label} ({token.prefix})"
    )
    return await _account_page(request, user, new_token=plain, new_token_label=token.label)


async def revoke_token(request: Request) -> Response:
    user = require_user(request)
    await checked_form(request)
    scout = scout_of(request)
    token_id = int(request.path_params["token_id"])
    if scout.store.revoke_token(user.id, token_id):
        scout.store.log_activity(
            user_id=user.id, car_id=None, action="token.revoke", detail=f"token {token_id}"
        )
        flash(request, "Token revoked. Anything using it has lost access.")
    else:
        flash(request, "That token was already revoked.", "error")
    return see_other("/account")


async def capture_download(request: Request) -> Response:
    scout = scout_of(request)
    capture_id = request.path_params["capture_id"]
    if not valid_capture_id(capture_id):
        raise HTTPException(404)
    signed = scout.capture_links.verify(
        capture_id, request.query_params.get("exp"), request.query_params.get("sig")
    )
    # Check who is asking before looking anything up, so strangers learn nothing.
    user = None if signed else current_user(request)
    if not signed and user is None:
        raise HTTPException(401, "Please sign in to download your captures.")
    folder = scout.robot.capture_dir(capture_id)
    if folder is None or not folder.is_dir():
        raise HTTPException(404, "That capture doesn't exist (any more).")
    if user is not None:
        meta = await anyio.to_thread.run_sync(read_meta, folder)
        mine = meta is not None and meta.user_id == user.id and user.status != "suspended"
        if not (user.admin or mine):
            raise HTTPException(404, "That capture doesn't exist (any more).")
    name = f"scout-{capture_id}"
    return StreamingResponse(
        iter_zip(folder, name),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.zip"',
            "Cache-Control": "private, no-store",
        },
    )


# ----------------------------------------------------------------------------- admin


def _car_view(scout: Scout, car_id: str) -> dict[str, Any]:
    car = scout.store.get_car(car_id)
    if car is None:
        raise HTTPException(404, "That car doesn't exist.")
    summary = scout.robot.car(car_id)
    driver = scout.lock.current(car_id)
    return {
        "car": car,
        "summary": summary,
        "summary_fields": dataclasses.asdict(summary) if summary is not None else None,
        "driver": driver,
        "driver_idle": int(driver.idle_for(scout.lock.now())) if driver is not None else 0,
        "telemetry_json": json.dumps(summary.telemetry, indent=1, default=str) if summary is not None else "",
    }


async def admin(request: Request) -> Response:
    user = require_admin(request)
    scout = scout_of(request)
    cars = []
    for car in scout.store.list_cars():
        summary = scout.robot.car(car.car_id)
        cars.append(
            {"car": car, "online": bool(summary and summary.online), "driver": scout.lock.current(car.car_id)}
        )
    return render(request, "admin.html", {"users": scout.store.list_users(), "cars": cars}, user=user)


USER_ACTIONS = {"approve", "suspend", "make-admin", "remove-admin"}


async def admin_user_action(request: Request) -> Response:
    admin_user = require_admin(request)
    await checked_form(request)
    scout = scout_of(request)
    action = request.path_params["action"]
    if action not in USER_ACTIONS:
        raise HTTPException(404)
    target = scout.store.get_user(int(request.path_params["user_id"]))
    if target is None:
        raise HTTPException(404, "That account doesn't exist.")
    try:
        if action == "approve":
            updated = scout.store.set_status(target.id, "active", by=admin_user)
            message = f"{updated.login} can use Scout now."
        elif action == "suspend":
            updated = scout.store.set_status(target.id, "suspended", by=admin_user)
            scout.lock.release_user(target.id)
            message = f"{updated.login} is suspended."
        elif action == "make-admin":
            updated = scout.store.set_admin(target.id, True, by=admin_user)
            message = f"{updated.login} is an admin now."
        else:
            updated = scout.store.set_admin(target.id, False, by=admin_user)
            message = f"{updated.login} is no longer an admin."
    except NotAllowed as exc:
        log_admin(request, admin_user, f"user.{action}", f"user={target.login}", outcome=f"refused: {exc}")
        flash(request, str(exc), "error")
        return see_other("/admin")
    log_admin(request, admin_user, f"user.{action}", f"user={target.login} (#{target.id})")
    flash(request, message)
    return see_other("/admin")


async def admin_add_car(request: Request) -> Response:
    admin_user = require_admin(request)
    form = await checked_form(request)
    scout = scout_of(request)
    car_id = form_text(form, "car_id").lower()
    name = " ".join(form_text(form, "name").split())[:40] or "Scout"
    if not CAR_ID_RE.match(car_id):
        flash(request, "A car id is 1-32 lowercase letters, digits and dashes, like scout-1.", "error")
        return see_other("/admin")
    try:
        car, key = scout.store.add_car(car_id, name)
    except NotAllowed as exc:
        flash(request, str(exc), "error")
        return see_other("/admin")
    log_admin(request, admin_user, "car.add", f"{car.car_id} ({car.name})", car_id=car.car_id)
    return render(
        request, "admin_car.html", {**_car_view(scout, car.car_id), "new_key": key}, user=admin_user
    )


async def admin_car(request: Request) -> Response:
    user = require_admin(request)
    return render(
        request,
        "admin_car.html",
        {**_car_view(scout_of(request), request.path_params["car_id"]), "new_key": None},
        user=user,
    )


def _int_field(form: FormData, name: str, low: int, high: int, label: str) -> int:
    text = form_text(form, name)
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"{label} must be a whole number.") from None
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return value


def _float_field(form: FormData, name: str, low: float, high: float, label: str) -> float:
    text = form_text(form, name)
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{label} must be a number.") from None
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low:g} and {high:g}.")
    return value


def settings_from_form(form: FormData) -> CarSettings:
    name = " ".join(form_text(form, "name").split())[:40]
    if not name:
        raise ValueError("Give the car a name.")
    return CarSettings(
        name=name,
        obst_cm=_int_field(form, "obst_cm", 5, 100, "Obstacle distance"),
        min_pwm=_int_field(form, "min_pwm", 0, 255, "Lowest motor PWM"),
        link_ms=_int_field(form, "link_ms", 300, 5000, "Link watchdog"),
        vflip=form.get("vflip") == "on",
        hmirror=form.get("hmirror") == "on",
        turn_deg_per_s=_float_field(form, "turn_deg_per_s", 1, 2000, "Turn speed"),
        forward_cm_per_s=_float_field(form, "forward_cm_per_s", 1, 500, "Forward speed"),
        strafe_cm_per_s=_float_field(form, "strafe_cm_per_s", 1, 500, "Sideways speed"),
    )


CAR_ACTIONS = {
    "rotate-key",
    "settings",
    "availability",
    "stop",
    "release",
    "disconnect",
    "firmware",
    "wheel-test",
}


async def admin_car_action(request: Request) -> Response:
    admin_user = require_admin(request)
    form = await checked_form(request)
    scout = scout_of(request)
    car_id = request.path_params["car_id"]
    action = request.path_params["action"]
    if action not in CAR_ACTIONS:
        raise HTTPException(404)
    car = scout.store.get_car(car_id)
    if car is None:
        raise HTTPException(404, "That car doesn't exist.")
    back = f"/admin/cars/{car_id}"
    detail = ""
    try:
        if action == "rotate-key":
            key = scout.store.rotate_car_key(car_id)
            log_admin(request, admin_user, "car.rotate_key", car_id, car_id=car_id)
            with contextlib.suppress(*CAR_ERRORS):
                await scout.robot.disconnect(car_id)
            return render(
                request, "admin_car.html", {**_car_view(scout, car_id), "new_key": key}, user=admin_user
            )
        if action == "settings":
            settings = settings_from_form(form)
            scout.store.update_settings(car_id, settings)
            detail = json.dumps(dataclasses.asdict(settings), sort_keys=True)
            try:
                await scout.robot.push_settings(car_id)
                flash(request, "Settings saved and sent to the car.")
            except CAR_ERRORS:
                flash(request, "Settings saved. The car gets them the next time it connects.")
        elif action == "availability":
            value = form_text(form, "availability")
            if value not in AVAILABILITIES:
                raise ValueError("Pick admins or everyone.")
            scout.store.set_availability(car_id, value)
            detail = value
            flash(
                request,
                "Only admins can drive it now."
                if value == "admins"
                else "Every approved user can drive it now.",
            )
        elif action == "stop":
            await scout.robot.stop_everything(car_id)
            flash(request, "Stopped.")
        elif action == "release":
            released = scout.lock.release(car_id)
            detail = f"driver={released.login}" if released else "nobody was driving"
            flash(request, f"{released.login} is no longer driving." if released else "Nobody was driving.")
        elif action == "disconnect":
            await scout.robot.disconnect(car_id)
            flash(request, "Disconnected. The car will reconnect by itself.")
        elif action == "firmware":
            url = form_text(form, "url")
            sha256 = form_text(form, "sha256").lower()
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname:
                raise ValueError("The firmware URL must start with https://.")
            if not SHA256_RE.match(sha256):
                raise ValueError("The SHA-256 must be 64 hex characters.")
            size = _int_field(form, "size", 1, MAX_FIRMWARE_BYTES, "Size")
            await scout.robot.update_firmware(car_id, url=url, sha256=sha256, size=size)
            # Keep any query string (it may hold a pre-signed token) out of the log.
            detail = f"url={parts.scheme}://{parts.netloc}{parts.path} sha256={sha256} size={size}"
            flash(request, "Firmware update started. Watch the OTA state below.")
        elif action == "wheel-test":
            bits = _int_field(form, "bits", 0, 255, "Bits")
            pwm5 = _int_field(form, "pwm5", 0, 255, "PWM D5")
            pwm6 = _int_field(form, "pwm6", 0, 255, "PWM D6")
            ms = _int_field(form, "ms", 1, 2000, "Duration")
            result = await scout.robot.wheel_test(car_id, bits=bits, pwm5=pwm5, pwm6=pwm6, ms=ms)
            detail = f"bits={bits} pwm5={pwm5} pwm6={pwm6} ms={ms}"
            flash(request, "Wheel test done: " + json.dumps(result.data, default=str)[:300])
    except ValueError as exc:
        flash(request, str(exc), "error")
        log_admin(request, admin_user, f"car.{action}", detail, car_id=car_id, outcome=f"refused: {exc}")
        return see_other(back)
    except CAR_ERRORS as exc:
        flash(request, str(exc) or "The car couldn't do that.", "error")
        log_admin(request, admin_user, f"car.{action}", detail, car_id=car_id, outcome=f"error: {exc}")
        return see_other(back)
    log_admin(request, admin_user, f"car.{action.replace('-', '_')}", detail, car_id=car_id)
    return see_other(back)


async def admin_activity(request: Request) -> Response:
    user = require_admin(request)
    scout = scout_of(request)
    selected: int | None = None
    raw = request.query_params.get("user", "")
    if raw.isdigit():
        selected = int(raw)
    return render(
        request,
        "admin_activity.html",
        {
            "entries": scout.store.recent_activity(limit=200, user_id=selected),
            "users": scout.store.list_users(),
            "selected": selected,
        },
        user=user,
    )


# ------------------------------------------------------------------------ assembly


def routes() -> list[BaseRoute]:
    return [
        Route("/", home, methods=["GET"]),
        Route("/install", install, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/i/{image_id}", image, methods=["GET"]),
        Route("/login", login, methods=["GET"]),
        Route("/auth/github/callback", github_callback, methods=["GET"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/account", account, methods=["GET"]),
        Route("/account/tokens", create_token, methods=["POST"]),
        Route("/account/tokens/{token_id:int}/revoke", revoke_token, methods=["POST"]),
        Route("/captures/{capture_id}.zip", capture_download, methods=["GET"]),
        Route("/admin", admin, methods=["GET"]),
        Route("/admin/users/{user_id:int}/{action}", admin_user_action, methods=["POST"]),
        Route("/admin/cars", admin_add_car, methods=["POST"]),
        Route("/admin/cars/{car_id}", admin_car, methods=["GET"]),
        Route("/admin/cars/{car_id}/{action}", admin_car_action, methods=["POST"]),
        Route("/admin/activity", admin_activity, methods=["GET"]),
    ]


_TITLES = {401: "Please sign in", 403: "Not allowed", 404: "Not found", 405: "Not allowed", 413: "Too big"}


async def http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    status = exc.status_code
    default = {404: "There's nothing here.", 405: "That doesn't work here."}.get(status, "That didn't work.")
    detail = (
        exc.detail
        if isinstance(exc.detail, str) and exc.detail and exc.detail != _phrase(status)
        else default
    )
    if request.url.path.startswith(("/i/", "/healthz")):
        return Response(detail, status_code=status, media_type="text/plain")
    response = message_page(request, _TITLES.get(status, "Something went wrong"), detail, status_code=status)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


def _phrase(status: int) -> str:
    from http import HTTPStatus

    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def exception_handlers() -> dict[Any, Callable[[Request, Exception], Awaitable[Response]]]:
    return {HTTPException: http_error}
