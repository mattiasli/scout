"""What happens on every MCP tool call, independent of the MCP SDK.

In order: resolve the account, pick the car, check who may use it, check the driver
lock, call the robot, then write the call to the activity log.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import anyio
import mcp_types as types

from scout.captures import CaptureLinks, read_meta
from scout.contracts import ActionResult, CarBusy, CarError, CarOffline, RobotAPI
from scout.db import Car, Store, User
from scout.driver import DriverLock
from scout.images import ImageStore
from scout.tools import TOOLS_BY_NAME, InvalidArguments, ToolSpec, validate_args

log = logging.getLogger(__name__)

WAITING = "Your Scout account is waiting for an admin to approve it."
SUSPENDED = "Your Scout account is suspended. Ask an admin if you think that's a mistake."
NO_ACCOUNT = "This Scout token doesn't belong to an account any more. Make a new one on the Scout website."
NO_CAR = "No car has been added to Scout yet. An admin can add one on the Scout website."
UNEXPECTED = "Something went wrong on the Scout server. Please try again in a moment."


@dataclass(frozen=True)
class Caller:
    """Who is calling, as established by the MCP endpoint's token check."""

    user_id: int
    token_id: int | None  # None for the bootstrap token


class Refused(Exception):
    """The call isn't allowed. The message is written for the AI."""


def busy_message(car: Car, login: str) -> str:
    return f"{car.name} is being driven by {login} right now. Try again in a couple of minutes."


def text_result(text: str, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=error)


class ToolService:
    def __init__(
        self,
        *,
        store: Store,
        robot: RobotAPI,
        lock: DriverLock,
        images: ImageStore,
        capture_links: CaptureLinks,
        public_url: str,
    ) -> None:
        self.store = store
        self.robot = robot
        self.lock = lock
        self.images = images
        self.capture_links = capture_links
        self.public_url = public_url

    async def call(self, caller: Caller, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        user = self.store.get_user(caller.user_id)
        car: Car | None = None
        detail = _detail(arguments)
        try:
            spec = TOOLS_BY_NAME.get(name)
            if spec is None:
                raise Refused(f"Scout has no tool called {name!r}.")
            if user is None:
                raise Refused(NO_ACCOUNT)
            if user.status == "waiting":
                raise Refused(WAITING)
            if user.status != "active":
                raise Refused(SUSPENDED)
            try:
                args = validate_args(spec, arguments)
            except InvalidArguments as exc:
                raise Refused(str(exc)) from None
            car = self.store.default_car()
            if car is None:
                raise Refused(NO_CAR)
            if spec.name == "release_car":
                # Always allowed, so nobody gets stuck holding a car they can no longer use.
                result = self._release(car, user)
            elif car.availability == "admins" and not user.admin:
                raise Refused(f"{car.name} is only open to admins right now. Please try again later.")
            else:
                self._check_lock(spec, car, user)
                if spec.name == "capture_photo" and args.get("capture_id"):
                    await self._check_capture_owner(str(args["capture_id"]), user)
                with self.lock.driving(car.car_id, user.id):
                    action = await self._run(spec, car.car_id, user, args)
                result = self._format(spec, car, user, action)
        except Refused as exc:
            self._log(user, car, name, detail, f"refused: {exc}")
            return text_result(str(exc), error=True)
        except (CarOffline, CarBusy, CarError) as exc:
            message = str(exc) or "The car couldn't do that."
            self._log(user, car, name, detail, f"error: {message}")
            return text_result(message, error=True)
        except Exception:
            log.exception("Tool %s failed", name)
            self._log(user, car, name, detail, "error: unexpected")
            return text_result(UNEXPECTED, error=True)
        self._log(user, car, name, detail, "ok")
        return result

    # ------------------------------------------------------------------- rules

    def _check_lock(self, spec: ToolSpec, car: Car, user: User) -> None:
        if not spec.needs_lock:
            return
        if spec.name == "stop":
            # Stopping never takes the lock: the driver and admins may always stop,
            # anyone may stop a car that nobody is driving.
            holder = self.lock.holder_other_than(car.car_id, user.id)
            if holder is not None and not user.admin:
                raise Refused(busy_message(car, holder.login))
            self.lock.touch(car.car_id, user.id)
            return
        holder = self.lock.acquire(car.car_id, user.id, user.login)
        if holder is not None:
            raise Refused(busy_message(car, holder.login))

    async def _check_capture_owner(self, capture_id: str, user: User) -> None:
        folder = self.robot.capture_dir(capture_id)
        if folder is None:
            return
        meta = await anyio.to_thread.run_sync(read_meta, folder)
        if meta is not None and meta.user_id != user.id and not user.admin:
            raise Refused("That capture belongs to someone else. Leave capture_id out to start a new one.")

    def _release(self, car: Car, user: User) -> types.CallToolResult:
        released = self.lock.release(car.car_id, user.id)
        if released is not None:
            text = f"You're no longer driving {car.name}. Anyone can drive it now."
        else:
            text = f"You weren't driving {car.name}, so there was nothing to release."
        return text_result(json.dumps({"released": released is not None, "message": text}))

    async def _run(self, spec: ToolSpec, car_id: str, user: User, a: dict[str, Any]) -> ActionResult:
        robot = self.robot
        match spec.name:
            case "status":
                return await robot.status(car_id)
            case "look":
                return await robot.look(car_id, tilt=a.get("tilt"), light=a.get("light"), size=a["size"])
            case "move":
                return await robot.move(
                    car_id, direction=a["direction"], ms=a["ms"], speed=a["speed"], look=a["look"]
                )
            case "turn":
                return await robot.turn(
                    car_id, direction=a["direction"], degrees=a["degrees"], speed=a["speed"], look=a["look"]
                )
            case "drive":
                return await robot.drive(
                    car_id, vx=a["vx"], vy=a["vy"], wz=a["wz"], ms=a["ms"], look=a["look"]
                )
            case "set_light":
                return await robot.set_light(car_id, level=a["level"])
            case "stop":
                return await robot.stop(car_id)
            case "look_around":
                return await robot.look_around(car_id, views=a["views"])
            case "capture_photo":
                return await robot.capture_photo(
                    car_id, user_id=user.id, capture_id=a.get("capture_id"), note=a.get("note")
                )
            case "capture_orbit":
                return await robot.capture_orbit(
                    car_id, user_id=user.id, photos=a["photos"], direction=a["direction"]
                )
            case "find":
                return await robot.find(car_id, target=a.get("target"), size=a["size"])
            case "follow":
                return await robot.follow(
                    car_id,
                    target=a["target"],
                    seconds=a["seconds"],
                    pick=a.get("pick"),
                    distance_cm=a.get("distance_cm"),
                )
            case "follow_view":
                return await robot.follow_view(car_id, wait_s=a["wait_s"])
        raise Refused(f"Scout has no tool called {spec.name!r}.")  # pragma: no cover

    # ----------------------------------------------------------------- results

    def _format(self, spec: ToolSpec, car: Car, user: User, action: ActionResult) -> types.CallToolResult:
        data: dict[str, Any] = dict(action.data)
        links = [
            f"{self.public_url}/i/{image_id}"
            for image_id in (self.images.put(image) for image in action.images)
            if image_id is not None
        ]
        if links:
            data["image_links"] = links
            data["image_links_expire_in_minutes"] = int(self.images.ttl // 60)
        if action.capture_id:
            data["capture_id"] = action.capture_id
            data["capture_download"] = self.capture_links.link(action.capture_id)
        if spec.name == "status":
            driver = self.lock.current(car.car_id)
            data["driver"] = (
                None if driver is None else {"login": driver.login, "you": driver.user_id == user.id}
            )
        content: list[types.ContentBlock] = [
            types.TextContent(text=json.dumps(data, ensure_ascii=False, default=str))
        ]
        content.extend(
            types.ImageContent(data=base64.b64encode(image).decode("ascii"), mime_type="image/jpeg")
            for image in action.images
        )
        return types.CallToolResult(content=content)

    def _log(self, user: User | None, car: Car | None, name: str, detail: str, outcome: str) -> None:
        try:
            self.store.log_activity(
                user_id=user.id if user is not None else None,
                car_id=car.car_id if car is not None else None,
                action=f"tool.{name}"[:60],
                detail=detail,
                outcome=outcome,
            )
        except Exception:
            log.exception("Couldn't write to the activity log")


def _detail(arguments: dict[str, Any] | None) -> str:
    if not arguments:
        return ""
    try:
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)[:500]
    except (TypeError, ValueError):
        return "(unreadable arguments)"
