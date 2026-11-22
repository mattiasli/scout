"""First-person camera images for the simulated car (numpy + Pillow).

A column raycaster with a depth buffer: every column casts one ray, and each wall, box,
disc and the cat is painted as a vertical span whose height comes from its distance.
Walls are pale, each in its own colour, with a door in one; furniture is blue, green and
grey; the floor is a grey checkerboard. The cat is the only orange thing in the room,
drawn as a side-on cartoon (body, head, ears, legs, tail, stripes), so a colour detector
can find it reliably and a person can recognise it.

Large resolutions are rendered at up to 1024 px wide and scaled up, like a soft lens.
"""

from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image

from scout_sim.world import WALL_COLORS, Scene, ray_box, ray_disc, ray_walls

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "qvga": (320, 240),
    "vga": (640, 480),
    "svga": (800, 600),
    "xga": (1024, 768),
    "hd": (1280, 720),
    "sxga": (1280, 1024),
    "uxga": (1600, 1200),
    "qxga": (2048, 1536),
}

FOV_DEG = 66.0  # horizontal, roughly an OV3660 with the stock lens
CAM_HEIGHT = 9.0  # cm above the floor
CAM_FORWARD = 10.0  # cm ahead of the car's turning centre
MAX_RENDER_WIDTH = 1024

CEILING = (226, 226, 220)
FLOOR_A = (128, 118, 106)
FLOOR_B = (113, 104, 95)
DOOR_CLOSED = (120, 85, 55)
DOOR_OPEN = (30, 30, 34)
DOOR_FRAME = (70, 55, 40)
CAT_BODY = (235, 135, 45)
CAT_STRIPE = (190, 95, 30)
CAT_DARK = (40, 35, 30)


def _span_mask(rows: np.ndarray, top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    return (rows >= top[None, :]) & (rows < bottom[None, :])


def render(
    scene: Scene,
    width: int,
    height: int,
    *,
    tilt_deg: float = 0.0,
    light: int = 0,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """An upright RGB image (height, width, 3) of what the camera sees."""
    rw = min(width, MAX_RENDER_WIDTH)
    rh = max(1, round(height * rw / width))
    img = _render(scene, rw, rh, tilt_deg, light, rng or np.random.default_rng())
    if (rw, rh) != (width, height):
        img = np.asarray(Image.fromarray(img).resize((width, height), Image.Resampling.BILINEAR))
    return img


def _render(
    scene: Scene, w: int, h: int, tilt_deg: float, light: int, rng: np.random.Generator
) -> np.ndarray:
    car = scene.car
    heading = car.heading
    ox = car.x + math.cos(heading) * CAM_FORWARD
    oy = car.y + math.sin(heading) * CAM_FORWARD
    f = (w / 2) / math.tan(math.radians(FOV_DEG / 2))
    offs = np.arctan((np.arange(w) + 0.5 - w / 2) / f)  # + = to the right
    angles = heading - offs
    cos_off = np.cos(offs)
    dx = np.cos(angles)
    dy = np.sin(angles)
    horizon = h / 2 + f * math.tan(math.radians(tilt_deg))
    rows = (np.arange(h, dtype=np.float32) + 0.5)[:, None]

    color = np.empty((h, w, 3), dtype=np.float32)
    color[:] = CEILING
    depth = np.full((h, w), np.inf, dtype=np.float32)

    # Floor: distance per row below the horizon, world position per pixel for the tiles.
    r_floor = int(min(h, max(0, math.ceil(horizon + 0.5))))
    if r_floor < h:
        below = rows[r_floor:] - horizon
        floor_d = (f * CAM_HEIGHT / np.maximum(below, 0.5)).astype(np.float32)  # (n, 1)
        along = floor_d / cos_off[None, :]
        tiles = (
            np.floor((ox + along * dx[None, :]) / 50) + np.floor((oy + along * dy[None, :]) / 50)
        ) % 2 == 0
        color[r_floor:] = np.where(
            tiles[..., None], np.array(FLOOR_A, np.float32), np.array(FLOOR_B, np.float32)
        )
        depth[r_floor:] = floor_d

    def paint(t: np.ndarray, top_cm: float, rgb: np.ndarray) -> None:
        """Paint a floor-standing object of height ``top_cm`` hit at distances ``t``.

        ``rgb`` is per column ``(w, 3)`` or per pixel ``(h, w, 3)``.
        """
        dp = t * cos_off
        valid = np.isfinite(dp) & (dp > 0.5)
        if not valid.any():
            return
        cols = np.nonzero(valid)[0]
        c0, c1 = int(cols[0]), int(cols[-1]) + 1
        dps = np.where(valid[c0:c1], dp[c0:c1], np.inf)
        top = horizon - f * (top_cm - CAM_HEIGHT) / dps
        bottom = horizon + f * CAM_HEIGHT / dps
        finite = np.isfinite(dps)
        r0 = int(max(0, math.floor(float(np.min(top[finite])))))
        r1 = int(min(h, math.ceil(float(np.max(bottom[finite])))))
        if r0 >= r1:
            return
        sub_depth = depth[r0:r1, c0:c1]
        rr = rows[r0:r1]
        mask = (rr >= top[None, :]) & (rr < bottom[None, :]) & (dps[None, :] < sub_depth)
        shape = (r1 - r0, c1 - c0, 3)
        src = rgb[r0:r1, c0:c1] if rgb.ndim == 3 else np.broadcast_to(rgb[None, c0:c1], shape)
        color[r0:r1, c0:c1][mask] = src[mask]
        sub_depth[mask] = np.broadcast_to(dps[None, :], mask.shape)[mask]

    # Walls, with the door.
    t, side, u = ray_walls(scene.width, scene.depth, ox, oy, dx, dy)
    wall_rgb = np.array(WALL_COLORS, np.float32)[side]  # (w, 3)
    door = scene.door
    if door is not None and bool(np.any(side == door.wall)):
        wall_px = np.ascontiguousarray(np.broadcast_to(wall_rgb[None, :, :], (h, w, 3)))
        dp = t * cos_off
        z = CAM_HEIGHT + (horizon - rows) * dp[None, :] / f  # height on the wall per pixel
        on_wall = (side == door.wall)[None, :]
        inside = on_wall & (u >= door.u0)[None, :] & (u <= door.u1)[None, :] & (z < door.height)
        edge = ((np.abs(u - door.u0) < 3) | (np.abs(u - door.u1) < 3))[None, :] & (z < door.height + 3)
        lintel = ((u >= door.u0 - 3) & (u <= door.u1 + 3))[None, :] & (np.abs(z - door.height) < 3)
        frame = on_wall & (edge | lintel)
        wall_px[inside] = DOOR_OPEN if door.open else DOOR_CLOSED
        wall_px[frame] = DOOR_FRAME
        paint(t, scene.wall_height, wall_px)
    else:
        paint(t, scene.wall_height, wall_rgb)

    for b in scene.boxes:
        tb, xface = ray_box(b, ox, oy, dx, dy)
        rgb = np.array(b.color, np.float32)[None, :] * np.where(xface, 1.0, 0.85)[:, None].astype(np.float32)
        paint(tb, b.height, rgb)
    for d in scene.discs:
        td = ray_disc(d.x, d.y, d.r, ox, oy, dx, dy)
        paint(td, d.height, np.broadcast_to(np.array(d.color, np.float32), (w, 3)))

    if scene.cat is not None:
        color, depth = _paint_cat(scene, color, depth, ox, oy, heading, f, horizon, w, h)

    # Lighting: the room light fades with distance; the car's LED lights up what is close.
    dist = np.minimum(depth, np.float32(600.0))
    shade = 0.62 + 0.38 * np.exp(-dist / 450.0) + (light / 100.0) * 0.5 * np.exp(-((dist / 90.0) ** 2))
    color *= shade[..., None]
    color += rng.standard_normal(size=color.shape, dtype=np.float32) * np.float32(2.5)
    return np.clip(color, 0, 255).astype(np.uint8)


def _paint_cat(
    scene: Scene,
    color: np.ndarray,
    depth: np.ndarray,
    ox: float,
    oy: float,
    heading: float,
    f: float,
    horizon: float,
    w: int,
    h: int,
) -> tuple[np.ndarray, np.ndarray]:
    cat = scene.cat
    assert cat is not None
    vx, vy = cat.x - ox, cat.y - oy
    fwd = vx * math.cos(heading) + vy * math.sin(heading)
    right = vx * math.sin(heading) - vy * math.cos(heading)
    if fwd < 5:
        return color, depth
    s = f / fwd  # pixels per cm at the cat's distance
    sx = w / 2 + f * right / fwd
    view = math.atan2(vy, vx)
    side_len = max(cat.length * abs(math.sin(cat.heading - view)), cat.length * 0.45)
    # Which way the head points on screen: the cat's heading on the camera's right axis.
    head_dir = (
        1.0
        if math.cos(cat.heading) * math.sin(heading) - math.sin(cat.heading) * math.cos(heading) >= 0
        else -1.0
    )
    feet = horizon + f * CAM_HEIGHT / fwd
    a = s * side_len * 0.36  # body half-length
    bh = s * cat.height * 0.28  # body half-height
    body_cy = feet - s * cat.height * 0.42
    head_r = s * cat.height * 0.22
    head_cx = sx + head_dir * a * 0.95
    head_cy = feet - s * cat.height * 0.72
    x0 = int(max(0, math.floor(sx - a - head_r * 2.5)))
    x1 = int(min(w, math.ceil(sx + a + head_r * 2.5)))
    y0 = int(max(0, math.floor(feet - s * cat.height * 1.15)))
    y1 = int(min(h, math.ceil(feet + 1)))
    if x0 >= x1 or y0 >= y1:
        return color, depth
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32) + 0.5
    body = ((xx - sx) / max(a, 0.5)) ** 2 + ((yy - body_cy) / max(bh, 0.5)) ** 2 <= 1
    head = (xx - head_cx) ** 2 + (yy - head_cy) ** 2 <= head_r**2
    ears = np.zeros_like(body)
    for side in (-0.55, 0.55):
        ex = head_cx + side * head_r
        tip = head_cy - head_r * 1.5
        ears |= (yy >= tip + np.abs(xx - ex) * 2.2) & (yy <= head_cy - head_r * 0.5)
    legs = np.zeros_like(body)
    for lx in (-0.7, -0.35, 0.35, 0.7):
        cx = sx + lx * a
        legs |= (np.abs(xx - cx) <= max(1.0, s * 1.6)) & (yy >= body_cy) & (yy <= feet)
    tail_cx = sx - head_dir * a * 1.05
    tail = ((xx - tail_cx) / max(s * 2.5, 0.8)) ** 2 + (
        (yy - (body_cy - bh * 1.3)) / max(bh * 1.4, 0.8)
    ) ** 2 <= 1
    shape = body | head | ears | legs | tail
    stripes = body & (np.sin((xx - sx) / max(s * 2.2, 0.6)) > 0.5)
    eyes = np.zeros_like(body)
    for ex in (-0.35, 0.35):
        eyes |= (xx - (head_cx + ex * head_r + head_dir * head_r * 0.25)) ** 2 + (yy - head_cy) ** 2 <= (
            head_r * 0.14
        ) ** 2
    region_depth = depth[y0:y1, x0:x1]
    visible = shape & (fwd * 1.0 < region_depth)
    rgb = np.where(stripes[..., None], np.array(CAT_STRIPE, np.float32), np.array(CAT_BODY, np.float32))
    rgb = np.where(eyes[..., None], np.array(CAT_DARK, np.float32), rgb)
    region = color[y0:y1, x0:x1]
    color[y0:y1, x0:x1] = np.where(visible[..., None], rgb, region)
    depth[y0:y1, x0:x1] = np.where(visible, fwd, region_depth)
    return color, depth


def jpeg_quality(q: int) -> int:
    """Map the camera's quality scale (4..63, lower is better) to Pillow's (10..95)."""
    return int(max(10, min(95, round(100 - (q - 4) * 1.4))))


def encode_jpeg(img: np.ndarray, q: int) -> bytes:
    out = io.BytesIO()
    Image.fromarray(img).save(out, "JPEG", quality=jpeg_quality(q))
    return out.getvalue()


def snapshot(
    scene: Scene,
    res: str,
    q: int = 12,
    *,
    tilt_deg: float = 0.0,
    light: int = 0,
    vflip: bool = True,
    hmirror: bool = False,
    seed: int | None = None,
) -> bytes:
    """A JPEG as the car's camera would send it.

    The ESP32-CAM-V2.0 sensor sits upside down, so its raw image needs ``vflip`` to be
    upright; the simulator copies that, which makes a missing ``conf`` easy to spot.
    """
    width, height = RESOLUTIONS[res]
    img = render(scene, width, height, tilt_deg=tilt_deg, light=light, rng=np.random.default_rng(seed))
    if not vflip:
        img = img[::-1]
    if hmirror:
        img = img[:, ::-1]
    return encode_jpeg(np.ascontiguousarray(img), q)
