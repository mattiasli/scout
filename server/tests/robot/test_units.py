"""Unit tests for the robot side's pure parts: maths, words, files, pictures, detection."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image

from scout.contracts import CarBusy, CarError, CarSettings
from scout.robot import InvalidRequest
from scout.robot.captures import CaptureStore, new_capture_id, valid_capture_id
from scout.robot.detector import (
    COCO_LABELS,
    YOLOX_NANO,
    Detection,
    DetectorUnavailable,
    ModelSpec,
    YoloxDetector,
    decode_yolox,
    default_detector_factory,
    ensure_model,
    normalise_label,
)
from scout.robot.errors import error_for_code
from scout.robot.follow import FollowTuning, command, pick_target
from scout.robot.imaging import jpeg_size, labelled_grid, preview
from scout.robot.motion import (
    PlannedMove,
    body_velocity,
    direction_velocity,
    duty_share,
    estimate,
    orbit_step,
    percent_to_pwm,
    radius_correction,
    speed_share,
    split_ms,
    turn_plan,
    turn_rate,
    wheel_mix,
    wheel_output,
    wheel_speeds,
)
from scout.robot.text import ago, heading_label, reason_phrase


def jpeg(width: int, height: int, color: tuple[int, int, int] = (90, 120, 150)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "JPEG")
    return out.getvalue()


# -- motion ----------------------------------------------------------------------------


def test_direction_vectors() -> None:
    assert direction_velocity("forward", 60) == (60, 0, 0)
    assert direction_velocity("back", 60) == (-60, 0, 0)
    assert direction_velocity("right", 60) == (0, 60, 0)
    assert direction_velocity("left", 60) == (0, -60, 0)
    assert direction_velocity("forward_right", 60) == (42, 42, 0)
    assert direction_velocity("forward_left", 60) == (42, -42, 0)
    assert direction_velocity("back_right", 100) == (-71, 71, 0)
    assert direction_velocity("back_left", 20) == (-14, -14, 0)


def test_mixing_follows_the_uno() -> None:
    assert wheel_mix(60, 0, 0) == (60, 60, 60, 60)
    assert wheel_mix(71, 71, 0) == (100, 0, 0, 100)  # a full-speed diagonal: 142 % scaled down
    assert wheel_mix(-100, 100, -100) == (-33, -100, -33, 33)  # truncated toward zero
    assert wheel_output(42, 42, 0) == (84, 0, 0, 84)  # diagonals drive two wheels
    assert wheel_output(40, 0, 30, "shared") == (70, 70, 0, 0)  # 10 < 70 / 4: the right side coasts
    assert wheel_output(25, 0, 15, "shared") == (40, 40, 40, 40)  # exactly a quarter is still driven
    assert wheel_output(50, 0, 20, "shared") == (70, 70, 70, 70)  # the shared duty evens them out
    assert wheel_output(50, 0, 20) == (70, 70, 30, 30)  # one pin per side: each side its own speed
    assert wheel_output(50, 0, 20, "ideal") == (70, 70, 30, 30)
    assert wheel_output(60, 40, 0, "split") == (100, 0, 0, 100)  # 25 % rule within each side
    assert body_velocity(wheel_output(20, 15, -10, "ideal")) == pytest.approx((20, 15, -10))


def test_motor_curve() -> None:
    # firmware percentToPwm (mecanum_test.cpp checks the same values): 0 stays 0, 1 % is
    # min_pwm, 100 % is 255.
    duties = [percent_to_pwm(x, 90) for x in (0, 1, 20, 30, 50, 60, 99, 100)]
    assert duties == [0, 90, 122, 138, 172, 188, 253, 255]
    assert percent_to_pwm(-50, 90) == 172
    # The measured curve: straight lines between the measured duties, none at 25 or below.
    assert duty_share(0) == duty_share(25) == 0.0
    assert duty_share(255) == duty_share(300) == 1.0
    assert duty_share(138) == pytest.approx(99.4 / 202)
    assert duty_share(226) == pytest.approx(166.5 / 202)
    assert duty_share(199) == pytest.approx((132 + 166.5) / 2 / 202)  # halfway from 172 to 226
    assert duty_share(-172) == duty_share(172)
    shares = [duty_share(d) for d in range(0, 256)]
    assert shares == sorted(shares)  # faster with more duty, never slower
    assert speed_share(100, 90) == 1.0
    assert speed_share(0, 90) == 0.0
    assert speed_share(50, 90) == pytest.approx(132 / 202)  # duty 172
    assert speed_share(-50, 90) == pytest.approx(-132 / 202)
    assert speed_share(20, 110) == pytest.approx(99.4 / 202)  # duty 138
    assert wheel_speeds((100, -50, 0, 1), 90) == pytest.approx(
        (100, -100 * 132 / 202, 0, 100 * duty_share(90))
    )
    # The floor runs on the AD176 against the calibration they give at 100 %: 202°/s
    # turning, 43.5 cm/s forward and 37 cm/s sideways. The turns are the curve's own
    # points; forward and sideways fit its shape within a few percent.
    for min_pwm, speed, deg_per_s in ((110, 20, 99.4), (90, 50, 132), (110, 80, 166.5), (110, 100, 202)):
        assert turn_rate(speed, CarSettings(min_pwm=min_pwm, turn_deg_per_s=202)) == pytest.approx(deg_per_s)
    s = CarSettings(min_pwm=90, forward_cm_per_s=43.5, strafe_cm_per_s=37, turn_deg_per_s=202)
    for speed, cm_per_s in ((20, 18.8), (30, 21.5), (50, 28.0)):
        assert estimate(speed, 0, 0, 1000, s).forward_cm == pytest.approx(cm_per_s, rel=0.03)
    assert estimate(0, 50, 0, 1000, s).right_cm == pytest.approx(24.25, rel=0.03)


def test_estimates_from_calibration() -> None:
    s = CarSettings()  # min_pwm 90; 30 cm/s forward, 20 cm/s sideways, 180°/s at 100 %
    s50, s60 = duty_share(172), duty_share(188)
    e = estimate(60, 0, 0, 1000, s)
    assert (e.forward_cm, e.right_cm, e.clockwise_deg) == pytest.approx((30 * s60, 0, 0))
    e = estimate(0, -50, 0, 2000, s)
    assert e.right_cm == pytest.approx(-40 * s50)
    e = estimate(42, 42, 0, 1000, s)  # a diagonal: FL and RR at 84 % (duty 228), the others still
    assert e.distance_cm == pytest.approx(math.hypot(30, 20) / 2 * duty_share(228))
    assert estimate(0, 0, 50, 1000, s).clockwise_deg == pytest.approx(180 * s50)
    assert estimate(100, 0, 100, 1000, s).forward_cm == pytest.approx(15)  # scaled to fit
    # A blend is estimated the way the shield drives it. With the shared duty all four
    # wheels run at 70 % (duty 205), so it goes straight; one pin per side keeps the turn
    # (the left side at 70 %, the right at 30 %, duty 138).
    e = estimate(50, 0, 20, 1000, s, layout="shared")
    assert (e.forward_cm, e.right_cm, e.clockwise_deg) == pytest.approx((30 * duty_share(205), 0, 0))
    e = estimate(50, 0, 20, 1000, s)
    left, right = duty_share(205), duty_share(138)
    assert (e.forward_cm, e.right_cm, e.clockwise_deg) == pytest.approx(
        (30 * (left + right) / 2, 0, 180 * (left - right) / 2)
    )
    assert estimate(50, 0, 20, 1000, s, layout="ideal") == e
    # A pure move is the same whatever the layout.
    for layout in ("shared", "split", "ideal"):
        assert estimate(42, -42, 0, 500, s, layout=layout) == estimate(42, -42, 0, 500, s)  # type: ignore[arg-type]


def test_split_and_turn_plans() -> None:
    assert split_ms(2500) == [2500]
    assert split_ms(6001) == [2001, 2000, 2000]
    assert all(c <= 3000 for c in split_ms(9999))
    assert sum(split_ms(9999)) == 9999
    s = CarSettings()
    assert turn_rate(50, s) == pytest.approx(180 * 132 / 202)  # duty 172
    speed, chunks = turn_plan(90, 50, s)
    assert speed == 50 and chunks == [round(90 / turn_rate(50, s) * 1000)]
    speed, chunks = turn_plan(720, 20, s)  # 76°/s (duty 122): 9.5 s in four chunks
    assert speed == 20 and sum(chunks) == round(720 / turn_rate(20, s) * 1000)
    assert len(chunks) == 4 and max(chunks) <= 3000
    speed, chunks = turn_plan(15, 100, s)  # 83 ms at full speed is too short: slow down
    assert 20 < speed < 100 and chunks[0] >= 150 and 15 / turn_rate(speed + 1, s) * 1000 < 150
    # 20 % still turns at 42 % of full speed, so a tiny angle stays a short pulse.
    speed, chunks = turn_plan(5, 100, s)
    assert speed == 20 and chunks == [round(5 / turn_rate(20, s) * 1000)]


def _drive(moves: list[PlannedMove], settings: CarSettings) -> tuple[float, float, float]:
    """Integrate planned moves exactly (ideal wheels on the motor curve). Returns x, y,
    heading (math frame)."""
    x = y = 0.0
    h = math.pi / 2  # facing +y
    for mv in moves:
        vx, vy, wz = body_velocity(wheel_speeds(wheel_output(mv.vx, mv.vy, mv.wz, "ideal"), settings.min_pwm))
        t = mv.ms / 1000
        steps = 200
        for _ in range(steps):
            dt = t / steps
            fwd = vx / 100 * settings.forward_cm_per_s * dt
            right = vy / 100 * settings.strafe_cm_per_s * dt
            turn = math.radians(wz / 100 * settings.turn_deg_per_s * dt)
            mid = h - turn / 2
            x += fwd * math.cos(mid) + right * math.sin(mid)
            y += fwd * math.sin(mid) - right * math.cos(mid)
            h -= turn
    return x, y, h


@pytest.mark.parametrize("mode", ["split", "blend"])
@pytest.mark.parametrize("direction", ["left", "right"])
def test_orbit_step_lands_on_the_circle_facing_the_object(mode: str, direction: str) -> None:
    s = CarSettings()
    radius = 60.0
    moves = orbit_step(radius, 30, direction, s, speed=40, mode=mode)  # type: ignore[arg-type]
    assert all(m.vx == 0 and m.ms <= 3000 for m in moves)
    if mode == "split":  # rotate, strafe (maybe in chunks), rotate; each a pure move
        assert moves[0].wz and moves[-1].wz and not moves[0].vy and not moves[-1].vy
        assert all(m.vy and not m.wz for m in moves[1:-1]) and len(moves) >= 3
    else:  # one constant blend, chunked; wz is rounded first so the sum can pass speed a bit
        assert len({(m.vy, m.wz) for m in moves}) == 1
        assert abs(moves[0].vy) + abs(moves[0].wz) <= 40 + 3
    x, y, h = _drive(moves, s)
    # The object is at (0, radius); the car must still be ``radius`` away and facing it.
    assert math.hypot(x, y - radius) == pytest.approx(radius, rel=0.03)
    facing = math.atan2(radius - y, -x)
    assert abs((h - facing + math.pi) % (2 * math.pi) - math.pi) < math.radians(2)
    assert (x < 0) == (direction == "left")


def test_blend_orbit_keeps_its_speed_and_falls_back_when_too_slow() -> None:
    fast = CarSettings(forward_cm_per_s=120, strafe_cm_per_s=100, turn_deg_per_s=720)
    moves = orbit_step(25, 90, "right", fast, speed=80, mode="blend")
    assert len({(m.vy, m.wz) for m in moves}) == 1
    assert 75 <= abs(moves[0].vy) + abs(moves[0].wz) <= 80  # the fastest wheel near 80 %
    # At 20 % no blend gets a 25 cm circle within 3 %: every running wheel turns at least
    # as fast as min_pwm drives it. It strafes and rotates in turn instead.
    s = CarSettings()
    assert orbit_step(25, 30, "right", s, speed=20, mode="blend") == orbit_step(25, 30, "right", s, speed=20)


def test_radius_correction() -> None:
    s = CarSettings()
    assert radius_correction(None, 50, s) is None
    assert radius_correction(52, 50, s) is None
    rate = 30 * 99.4 / 202  # 30 % is duty 138
    fix = radius_correction(60, 50, s)
    assert fix is not None and fix.vx > 0 and fix.ms == pytest.approx(10 / rate * 1000, abs=1)
    fix = radius_correction(10, 50, s)
    # Capped at 15 cm.
    assert fix is not None and fix.vx < 0 and fix.ms == pytest.approx(15 / rate * 1000, abs=1)


# -- words -----------------------------------------------------------------------------


def test_phrases() -> None:
    assert ago(2) == "just now"
    assert ago(42) == "42 seconds ago"
    assert ago(61) == "a minute ago"
    assert ago(300) == "5 minutes ago"
    assert ago(7200) == "2 hours ago"
    assert ago(3 * 86400) == "3 days ago"
    assert reason_phrase("time", 50, 40, 800) == "completed"
    assert reason_phrase("obstacle", 30, 12, 400) == "stopped early: something 12 cm ahead"
    assert reason_phrase("obstacle", 9, 9, 0) == "didn't move: something 9 cm ahead"
    assert reason_phrase("obstacle", None, None, 10) == "stopped early: something too close ahead"
    assert reason_phrase("stopped", 1, 1, 1) == "stopped early: stop was requested"
    assert reason_phrase("replaced", 1, 1, 1) == "stopped early: replaced by a newer command"
    assert heading_label(0) == "0° ahead"
    assert heading_label(90) == "90° right"
    assert heading_label(180) == "180° behind"
    assert heading_label(270) == "90° left"
    assert heading_label(359.8) == "0° ahead"


def test_err_codes_map_to_contract_exceptions() -> None:
    assert isinstance(error_for_code("busy", "moving", "Scout"), CarBusy)
    for code in ("uno", "camera", "args", "timeout", "unknown_type", "ota", "weird"):
        exc = error_for_code(code, "x\x00y", "Scout")
        assert isinstance(exc, CarError) and not isinstance(exc, CarBusy)
        assert "\x00" not in str(exc)
    assert str(error_for_code("camera", None, "Scout")) == "Scout's camera failed to take the picture."
    assert issubclass(InvalidRequest, CarError)


# -- captures --------------------------------------------------------------------------


def test_capture_ids() -> None:
    ids = {new_capture_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(valid_capture_id(i) and len(i) >= 12 for i in ids)
    for bad in ("", "short", "../../etc/passwd", "a b c d e f g h", "x" * 65, None, 5, "ab/cdefghijklmnop"):
        assert not valid_capture_id(bad)


def test_capture_store(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "captures")
    capture_id = store.create("car-1", 4)
    entry, count = store.add_photo(
        capture_id, jpeg(64, 48), width=64, height=48, note="hi", cm=33, heading_deg=12.345
    )
    assert count == 1 and entry["file"] == "001.jpg" and entry["heading_deg"] == 12.3
    _, count = store.add_photo(capture_id, jpeg(64, 48), width=64, height=48)
    assert count == 2
    folder = store.directory(capture_id)
    assert folder == tmp_path / "captures" / capture_id
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["photos"][1] == {
        "file": "002.jpg",
        "time": meta["photos"][1]["time"],
        "note": None,
        "cm": None,
        "heading_deg": None,
        "width": 64,
        "height": 48,
    }
    assert not list(folder.glob("*.tmp"))
    store.check_owner(capture_id, "car-1", 4)
    with pytest.raises(InvalidRequest, match="another car"):
        store.check_owner(capture_id, "car-2", 4)
    with pytest.raises(InvalidRequest, match="someone else"):
        store.check_owner(capture_id, "car-1", 5)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "meta.json").write_text("{}")
    assert store.directory("../secret") is None
    assert store.directory("..") is None


# -- pictures --------------------------------------------------------------------------


def test_grid_and_preview() -> None:
    tiles = [(jpeg(640, 480), f"{i * 45}°") for i in range(8)]
    grid = labelled_grid(tiles, columns=4, max_width=1280)
    w, h = jpeg_size(grid)
    assert w <= 1280 and h < w
    w, _ = jpeg_size(labelled_grid([(b"broken", "x"), (jpeg(640, 480), "y")], columns=2))
    assert w <= 1280
    assert jpeg_size(preview(jpeg(2048, 1536), max_width=480)) == (480, 360)
    assert jpeg_size(preview(jpeg(320, 240), max_width=480)) == (320, 240)
    with pytest.raises(ValueError):
        jpeg_size(b"\xff\xd8 not really")


# -- detection -------------------------------------------------------------------------


def test_labels_and_aliases() -> None:
    assert len(COCO_LABELS) == 80 and COCO_LABELS[15] == "cat" and COCO_LABELS[16] == "dog"
    assert normalise_label(" Kitty ") == "cat"
    assert normalise_label("Puppy") == "dog"
    assert normalise_label("human") == "person"
    assert normalise_label("teddy_bear") == "teddy bear"


def test_decode_yolox_finds_a_box_and_suppresses_duplicates() -> None:
    size = 416
    rows = sum((size // s) ** 2 for s in (8, 16, 32))
    out = np.zeros((rows, 85), np.float32)
    # Stride-32 grid starts after the stride-8 and stride-16 cells; cell (x=6, y=6).
    base = (size // 8) ** 2 + (size // 16) ** 2
    cell = base + 6 * 13 + 6
    out[cell, :4] = [0.5, 0.5, math.log(4.0), math.log(3.0)]  # centre (208, 208), 128×96 in the input
    out[cell, 4] = 0.9
    out[cell, 5 + 15] = 0.95  # cat
    out[cell + 1] = out[cell]
    out[cell + 1, 0] = -0.45  # nearly the same box from the neighbouring cell
    dets = decode_yolox(out, input_size=size, ratio=416 / 640, image_size=(640, 480))
    assert len(dets) == 1
    d = dets[0]
    assert d.label == "cat" and d.score == pytest.approx(0.855)
    # The letterbox keeps the image at the top left: input / 0.65 gives image pixels.
    # x: (144..272) → 221.5..418.5 of 640; y: (160..256) → 246.2..393.8 of 480.
    assert (d.x0, d.y0, d.x1, d.y1) == pytest.approx((0.3462, 0.5128, 0.6538, 0.8205), abs=1e-3)
    with pytest.raises(ValueError):
        decode_yolox(out[:10], input_size=size, ratio=1, image_size=(416, 416))


def _spec(payload: bytes) -> ModelSpec:
    return ModelSpec(
        name="test",
        file="model.onnx",
        url="https://models.invalid/model.onnx",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        input_size=416,
        licence="MIT",
        source="test",
    )


def test_model_download_is_verified(tmp_path: Path) -> None:
    payload = b"onnx-bytes" * 100
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    path = ensure_model(_spec(payload), tmp_path / "models", client=client)
    assert path.read_bytes() == payload
    assert requests == ["https://models.invalid/model.onnx"]
    ensure_model(_spec(payload), tmp_path / "models", client=client)  # cached: no second download
    assert len(requests) == 1

    tampered = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"x" * len(payload)))
    )
    with pytest.raises(DetectorUnavailable, match="doesn't match its pinned SHA-256"):
        ensure_model(_spec(payload), tmp_path / "other", client=tampered)
    assert not (tmp_path / "other" / "model.onnx").exists()

    missing = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))
    with pytest.raises(DetectorUnavailable, match="HTTP 404"):
        ensure_model(_spec(payload), tmp_path / "third", client=missing)

    def offline(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    with pytest.raises(DetectorUnavailable, match="couldn't be downloaded"):
        ensure_model(
            _spec(payload), tmp_path / "fourth", client=httpx.Client(transport=httpx.MockTransport(offline))
        )


def test_pinned_model_spec() -> None:
    assert YOLOX_NANO.url.startswith("https://github.com/Megvii-BaseDetection/YOLOX/releases/download/")
    assert YOLOX_NANO.licence == "Apache-2.0"
    assert YOLOX_NANO.size == 3_659_407
    assert len(YOLOX_NANO.sha256) == 64


def test_broken_model_file_is_unavailable(tmp_path: Path) -> None:
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"not a model")
    with pytest.raises(DetectorUnavailable, match="couldn't be loaded"):
        YoloxDetector(bad)


def test_detector_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_FOLLOW_DETECTOR", "sim-cat")
    assert type(default_detector_factory(tmp_path)()).__name__ == "SimCatDetector"
    monkeypatch.setenv("SCOUT_FOLLOW_DETECTOR", "yolox")
    offline = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(503)))
    monkeypatch.setattr("scout.robot.detector.httpx.Client", lambda **_kw: offline)
    with pytest.raises(DetectorUnavailable, match="HTTP 503"):
        default_detector_factory(tmp_path)()  # YOLOX: tries the pinned download


@pytest.mark.skipif(
    not os.environ.get("SCOUT_TEST_YOLOX"), reason="set SCOUT_TEST_YOLOX to a yolox_nano.onnx"
)
def test_real_yolox_model_runs() -> None:
    detector = YoloxDetector(Path(os.environ["SCOUT_TEST_YOLOX"]))
    assert "cat" in detector.labels
    assert isinstance(detector.detect(jpeg(640, 480)), list)


# -- follow controller -----------------------------------------------------------------


def box(
    cx: float, height: float, *, label: str = "cat", score: float = 0.9, bottom: float = 0.8
) -> Detection:
    return Detection(label, score, cx - 0.1, bottom - height, cx + 0.1, bottom)


def test_follow_command() -> None:
    assert command(box(0.5, 0.45), cm=80, obst_cm=15) == (0, 0, 0)  # centred, right size
    vx, _, wz = command(box(0.5, 0.15), cm=200, obst_cm=15)
    assert 25 <= vx <= 45 and wz == 0  # far: drive
    vx, _, wz = command(box(0.5, 0.75), cm=30, obst_cm=15)
    assert -30 <= vx <= -25 and wz == 0  # too close: back off
    vx, _, wz = command(box(0.95, 0.2), cm=200, obst_cm=15)
    assert vx == 0 and 25 <= wz <= 40  # well off to the right: turn clockwise first
    vx, _, wz = command(box(0.1, 0.45), cm=200, obst_cm=15)
    assert wz < 0
    assert command(box(0.5, 0.15), cm=20, obst_cm=15)[0] == 0  # never closer than obst + clearance
    assert command(box(0.5, 0.2, bottom=0.99), cm=200, obst_cm=15)[0] == 0  # cut off at the bottom
    vx, _, wz = command(box(0.7, 0.15), cm=200, obst_cm=15, tuning=FollowTuning(blend=True))
    assert vx > 0 and wz > 0


def test_pick_target_tracks_the_previous_one() -> None:
    a, b = box(0.2, 0.3), box(0.8, 0.5)
    assert pick_target([a, b, box(0.5, 0.9, label="dog")], "cat", None, min_score=0.3) is b  # biggest
    assert pick_target([a, b], "cat", box(0.25, 0.3), min_score=0.3) is a  # nearest to last
    assert pick_target([box(0.5, 0.5, score=0.2)], "cat", None, min_score=0.3) is None
