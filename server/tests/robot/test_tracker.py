"""Keeping to one target by its look, searching for it, and the pictures follow shows."""

from __future__ import annotations

import io
import math

import numpy as np
import pytest
from PIL import Image, ImageDraw

from scout.contracts import CarSettings
from scout.robot.detector import Detection
from scout.robot.follow import (
    FollowTuning,
    Search,
    _direction,
    _pixel,
    bearing_deg,
    exit_side,
    expected_position,
    keep_distance,
    looks_far,
    plan,
)
from scout.robot.imaging import GREEN, YELLOW, annotate, crop, jpeg_size
from scout.robot.tracker import (
    Appearance,
    choose,
    cut_off,
    describe,
    open_picture,
    overlap,
    similarity,
    size_change,
)

RED, BLUE, OLIVE, MUSTARD = (200, 30, 30), (30, 40, 160), (40, 160, 60), (220, 200, 40)
LEFT = (0.10, 0.10, 0.36, 0.95)
RIGHT = (0.60, 0.10, 0.86, 0.95)


def person(box: tuple[float, float, float, float], score: float = 0.9) -> Detection:
    return Detection("person", score, *box)


def scene(*people: tuple[tuple[float, float, float, float], tuple[int, int, int], tuple[int, int, int]],
          dim: float = 1.0) -> Image.Image:  # fmt: skip
    """A grey room with people drawn as a coloured top over coloured trousers."""
    rng = np.random.default_rng(1)
    noise = rng.integers(95, 125, size=(240, 320, 1))
    pixels = np.repeat(noise, 3, axis=2).astype(np.uint8)
    img = Image.fromarray(pixels, "RGB")
    draw = ImageDraw.Draw(img)
    for (x0, y0, x1, y1), top, bottom in people:
        middle = (y0 + y1) / 2
        draw.rectangle(
            (x0 * 320 + 12, y0 * 240, x1 * 320 - 12, middle * 240), fill=tuple(round(c * dim) for c in top)
        )
        draw.rectangle(
            (x0 * 320 + 16, middle * 240, x1 * 320 - 16, y1 * 240), fill=tuple(round(c * dim) for c in bottom)
        )
    return img


def jpeg(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=80)
    return out.getvalue()


# ------------------------------------------------------------------ appearance


def test_it_tells_two_people_apart_by_their_clothes() -> None:
    first = scene((LEFT, RED, BLUE), (RIGHT, OLIVE, MUSTARD))
    me = Appearance.of(describe(first, person(LEFT)))
    # They swap places: the one picked is now on the right, the other where it was.
    later = open_picture(jpeg(scene((LEFT, OLIVE, MUSTARD), (RIGHT, RED, BLUE))))
    candidates = [person(LEFT), person(RIGHT)]
    descriptions = [describe(later, d) for d in candidates]
    assert me.match(descriptions[1]) > 0.75
    assert me.match(descriptions[0]) < 0.45
    choice = choose(candidates, descriptions, me, expected=(0.23, 0.5), min_match=0.45)
    assert choice.index == 1  # by its look, although the other is where it was
    assert not choice.unsure
    assert choice.why.startswith("match ")
    assert len(choice.matches) == 2


def test_a_darker_picture_still_matches() -> None:
    me = Appearance.of(describe(scene((LEFT, RED, BLUE)), person(LEFT)))
    dim = describe(open_picture(jpeg(scene((LEFT, RED, BLUE), dim=0.7))), person(LEFT))
    assert me.match(dim) > 0.6


def test_someone_else_isnt_taken_for_it() -> None:
    me = Appearance.of(describe(scene((LEFT, RED, BLUE)), person(LEFT)))
    other = open_picture(jpeg(scene((RIGHT, OLIVE, MUSTARD))))
    choice = choose([person(RIGHT)], [describe(other, person(RIGHT))], me, None, min_match=0.45)
    assert choice.index is None
    assert "none of the 1 in view looks like it" in choice.why
    assert choice.match is not None and choice.match < 0.45


def test_lookalikes_are_told_apart_by_position_and_flagged() -> None:
    img = scene((LEFT, RED, BLUE), (RIGHT, RED, BLUE))
    me = Appearance.of(describe(img, person(LEFT)))
    candidates = [person(LEFT), person(RIGHT)]
    descriptions = [describe(img, d) for d in candidates]
    near_right = choose(candidates, descriptions, me, expected=(0.75, 0.5), min_match=0.45, margin=0.5)
    assert near_right.index == 1
    assert near_right.unsure
    assert "another one in view looks almost as alike" in near_right.why
    assert choose(candidates, descriptions, me, expected=(0.2, 0.5), min_match=0.45).index == 0


def test_the_same_colours_the_other_way_round_dont_match() -> None:
    me = Appearance.of(describe(scene((LEFT, RED, BLUE)), person(LEFT)))
    swapped = describe(scene((LEFT, BLUE, RED)), person(LEFT))
    assert 0.2 < me.match(swapped) < 0.45  # only the whole-box colours agree


def test_a_closer_view_cut_off_at_the_bottom_still_matches() -> None:
    me = Appearance.of(describe(scene((LEFT, RED, BLUE)), person(LEFT)))
    # Closer: the picture shows the top and only the start of the trousers.
    near = scene(((0.05, 0.0, 0.45, 1.6), RED, BLUE))
    cut = person((0.05, 0.0, 0.45, 1.0))
    assert me.match(describe(near, cut)) > 0.45


def test_without_a_pick_it_takes_the_biggest() -> None:
    small = Detection("cat", 0.9, 0.1, 0.5, 0.2, 0.6)
    big = Detection("cat", 0.6, 0.5, 0.3, 0.9, 0.9)
    choice = choose([small, big], [np.zeros(60), np.zeros(60)], None, None, min_match=0.45)
    assert (choice.index, choice.match, choice.matches) == (1, None, ())
    assert choose([], [], None, None, min_match=0.45).why == "none in view"


def test_the_look_adapts_slowly() -> None:
    a = describe(scene((LEFT, RED, BLUE)), person(LEFT))
    b = describe(scene((LEFT, OLIVE, MUSTARD)), person(LEFT))
    me = Appearance.of(a)
    me.learn(b)
    assert similarity(me.reference, a) == pytest.approx(1.0)  # the original is kept as it was
    assert similarity(me.recent, a) > similarity(me.recent, b)
    assert me.match(a) == pytest.approx(1.0)


def test_unreadable_pictures_are_refused() -> None:
    with pytest.raises(ValueError, match="unreadable"):
        open_picture(b"not a jpeg")


# ------------------------------------------------------------------------ size

# On the floor, a dark chair a quarter as wide as the person followed matched their dark
# clothes at 0.95. Here both look exactly alike.
CHAIR = (0.10, 0.40, 0.16, 0.60)


def test_a_look_alike_far_narrower_than_the_target_was_isnt_taken() -> None:
    look = describe(scene((LEFT, RED, BLUE)), person(LEFT))
    me = Appearance.of(look)
    last = person(RIGHT)  # 0.26 wide, a moment ago
    both = choose(
        [person(CHAIR), person(RIGHT)], [look, look], me, (0.13, 0.5),
        min_match=0.45, last=last, max_size_change=2.5,
    )  # fmt: skip
    assert both.index == 1  # though the chair is where the target was expected
    assert both.notes[0] is not None and "4.3× smaller than the target was" in both.notes[0]
    assert both.notes[1] is None
    alone = choose([person(CHAIR)], [look], me, None, min_match=0.45, last=last, max_size_change=2.5)
    assert alone.index is None
    assert alone.why == (
        "the one in view that looks most like it (match 1.00) is 4.3× smaller than the target was, "
        "too big a change to be it"
    )
    # Out of sight long enough, the allowance has grown, and it may be the target farther away.
    later = choose([person(CHAIR)], [look], me, None, min_match=0.45, last=last, max_size_change=5.0)
    assert later.index == 0
    # Nothing to compare with: no size check.
    assert choose([person(CHAIR)], [look], me, None, min_match=0.45, max_size_change=2.5).index == 0


def test_the_nearer_size_wins_between_look_alikes() -> None:
    look = describe(scene((LEFT, RED, BLUE)), person(LEFT))
    me = Appearance.of(look)
    same = person(RIGHT)
    wider = person((0.05, 0.05, 0.55, 0.95))  # 1.4 times as big: allowed, but less likely
    choice = choose([wider, same], [look, look], me, None, min_match=0.45, last=same, max_size_change=2.5)
    assert choice.index == 1


def test_the_one_the_detector_is_surer_of_wins_between_look_alikes() -> None:
    # On the floor, a coat rack hung with dark clothes was taken for a person as big as the
    # one followed, its colours as alike, but the detector was far less sure of it.
    look = describe(scene((LEFT, RED, BLUE)), person(LEFT))
    me = Appearance.of(look)
    me_now, rack = person(LEFT, score=0.85), person(RIGHT, score=0.47)
    tied = choose([me_now, rack], [look, look], me, None, min_match=0.45)
    assert tied.unsure  # alike in every other way
    choice = choose([me_now, rack], [look, look], me, None, min_match=0.45, typical_score=0.85)
    assert choice.index == 0 and not choice.unsure


def test_a_box_on_part_of_the_target_is_no_rival() -> None:
    # Close up, the detector boxed a person and their legs as well.
    img = scene((LEFT, RED, BLUE))
    me = Appearance.of(describe(img, person(LEFT)))
    body, legs = person(LEFT), person((0.14, 0.55, 0.32, 0.95), score=0.7)
    assert overlap(body, legs) == pytest.approx(1.0) and overlap(legs, body) == pytest.approx(1.0)
    assert overlap(body, person(RIGHT)) == 0.0
    look = describe(img, body)
    choice = choose([body, legs], [look, look], me, None, min_match=0.45, typical_score=0.85, margin=0.5)
    assert choice.index == 0 and not choice.unsure
    assert choose([body, person(RIGHT)], [look, look], me, None, min_match=0.45, margin=0.5).unsure


def test_cut_off() -> None:
    assert not cut_off(person(LEFT))
    assert cut_off(person((0.3, 0.0, 0.7, 0.9)))  # head out of the picture
    assert cut_off(person((0.3, 0.1, 0.7, 1.0)))  # close up, cut off at the knees
    assert cut_off(person((0.0, 0.1, 0.2, 0.9)))  # stepping out on the left
    # The detector's box often stops a little short of the edge.
    assert cut_off(person((0.3, 0.03, 0.7, 0.96)))  # close up, the head out of the picture
    assert cut_off(person((0.04, 0.1, 0.3, 0.9)))
    assert cut_off(person((0.7, 0.1, 0.96, 0.9)))
    # The camera sits low: in full view at 2-3 m on the floor, feet came within 4% of the
    # bottom, which isn't cut off.
    assert not cut_off(person((0.45, 0.26, 0.62, 0.96)))
    assert not cut_off(person((0.40, 0.31, 0.57, 0.97)))


def test_a_box_cut_off_at_an_edge_may_be_bigger_than_it_looks() -> None:
    whole = person((0.40, 0.1, 0.66, 0.9))
    at_the_edge = person((0.0, 0.1, 0.08, 0.9))  # stepping out of the picture on the left
    assert size_change(at_the_edge, whole) == 0.0
    assert size_change(whole, at_the_edge) == 0.0  # back in view: as wide as it really is
    knees = person((0.40, 0.3, 0.66, 1.0))  # the camera tilted up: cut off at the knees
    assert size_change(knees, whole) == 0.0
    assert size_change(person(CHAIR), whole) == pytest.approx(
        (math.log(0.06 / 0.26) + math.log(0.2 / 0.8)) / 2
    )
    side_on = Detection("cat", 0.9, 0.3, 0.5, 0.6, 0.7)
    facing = Detection("cat", 0.9, 0.4, 0.5, 0.52, 0.7)  # 2.5 times narrower, as tall
    assert math.exp(-size_change(facing, side_on)) == pytest.approx(math.sqrt(2.5))


# -------------------------------------------------------------------- geometry


@pytest.mark.parametrize("tilt", [0.0, 20.0, -15.0])
def test_pixel_is_the_inverse_of_direction(tilt: float) -> None:
    for x, y in [(0.5, 0.5), (0.1, 0.2), (0.9, 0.8), (0.3, 0.95)]:
        yaw, elevation = _direction(x, y, 81.0, tilt)
        back = _pixel(yaw, elevation, 81.0, tilt)
        assert back is not None
        assert back == pytest.approx((x, y), abs=1e-9)
    assert _pixel(180.0, 0.0, 81.0, 0.0) is None  # behind the camera


def test_where_the_target_should_be_after_a_turn() -> None:
    target = Detection("person", 0.9, 0.7, 0.3, 0.9, 0.9)  # off to the right
    turn = bearing_deg(target, 81.0, 10.0)
    after = expected_position(target, 81.0, tilt_before=10.0, tilt_after=10.0, turned_deg=turn)
    assert after is not None
    assert after[0] == pytest.approx(0.5, abs=1e-6)  # turned to face it
    still = expected_position(target, 81.0, tilt_before=10.0, tilt_after=10.0, turned_deg=0.0)
    assert still == pytest.approx((target.cx, target.cy))
    up = expected_position(target, 81.0, tilt_before=10.0, tilt_after=20.0, turned_deg=0.0)
    assert up is not None and up[1] > target.cy  # tilting up moves it down the picture


def test_exit_side() -> None:
    assert exit_side(Detection("cat", 0.9, 0.0, 0.4, 0.15, 0.7)) == "left"
    assert exit_side(Detection("cat", 0.9, 0.85, 0.4, 1.0, 0.7)) == "right"
    assert exit_side(Detection("person", 0.9, 0.4, 0.0, 0.6, 0.4)) == "top"
    assert exit_side(Detection("cat", 0.9, 0.4, 0.7, 0.6, 1.0)) == "bottom"
    assert exit_side(Detection("cat", 0.9, 0.4, 0.4, 0.6, 0.6)) == "middle"
    # A tall person filling the picture top to bottom left by neither end.
    assert exit_side(Detection("person", 0.9, 0.4, 0.0, 0.6, 1.0)) == "middle"
    # Walking right, it carries on out of the picture on the right. On the floor a person
    # close by moved from the middle to 0.51-0.87 in one frame and then wasn't seen again.
    walking = Detection("person", 0.9, 0.51, 0.0, 0.87, 0.94)
    assert exit_side(walking) == "middle"
    assert exit_side(walking, drift=0.18) == "right"
    assert exit_side(Detection("cat", 0.9, 0.2, 0.4, 0.4, 0.6), drift=-0.15) == "left"
    assert exit_side(Detection("cat", 0.9, 0.4, 0.4, 0.6, 0.6), drift=0.02) == "middle"  # jitter


def test_holding_back_still_turns_but_doesnt_drive() -> None:
    settings = CarSettings()
    far = person((0.45, 0.1, 0.55, 0.9))
    assert plan(far, 200, settings, hfov_deg=81.0).vx > 0
    held = plan(far, 200, settings, hfov_deg=81.0, hold="not sure")
    assert (held.vx, held.wz, held.ms, held.why) == (0, 0, 0, "hold: not sure")
    off_centre = person((0.80, 0.1, 0.90, 0.9))
    assert plan(off_centre, 200, settings, hfov_deg=81.0, hold="not sure").wz > 0


def test_the_ai_can_set_the_distance_to_keep() -> None:
    settings = CarSettings()
    default = FollowTuning()
    two_metres = keep_distance(default, 240)
    assert two_metres.target_width == pytest.approx(0.13)  # half as wide as at 1.2 m
    assert two_metres.target_height == pytest.approx(0.1125)  # a cat a quarter as tall as at 60 cm
    there = person((0.435, 0.1, 0.565, 0.9))  # 0.13 wide: about 2.4 m away
    assert plan(there, 400, settings, hfov_deg=81.0).vx > 0  # too far for the 1.2 m it keeps
    held = plan(there, 400, settings, hfov_deg=81.0, tuning=two_metres)
    assert held.vx == 0 and "about the right distance away" in held.why
    assert not looks_far(there, hfov_deg=81.0, tuning=two_metres)
    close = plan(there, 400, settings, hfov_deg=81.0, tuning=keep_distance(default, 400))
    assert close.vx < 0  # 4 m wanted: back off


def test_looks_far() -> None:
    assert looks_far(person((0.45, 0.1, 0.55, 0.9)), hfov_deg=81.0)
    assert not looks_far(person((0.37, 0.1, 0.63, 0.9)), hfov_deg=81.0)  # the width follow keeps
    assert not looks_far(person((0.3, 0.0, 0.7, 1.0)), hfov_deg=81.0)


# ---------------------------------------------------------------------- search


def steps(
    search: Search, cm: int | None = 150, tilt: float = 0.0
) -> list[tuple[int, int, int, int | None, str]]:
    settings = CarSettings()
    out = []
    while (
        step := search.next_step(cm, settings, tilt_deg=tilt, tuning=FollowTuning(search_deg=120))
    ) is not None:
        out.append((step.vx, step.wz, step.ms, step.tilt, step.why))
        if step.tilt is not None:
            tilt = step.tilt
        assert len(out) < 20
    return out


def test_search_turns_towards_the_side_it_went_out() -> None:
    left = steps(Search("cat", "left", -35.0))
    assert [s[1] < 0 for s in left] == [True, True, True]  # three 40° turns to the left
    assert left[0][4] == "search: turn 40° left, where the cat went out of the picture (40° of 120°)"
    assert left[-1][4].endswith("(120° of 120°)")
    right = steps(Search("cat", "right", 35.0))
    assert all(s[1] > 0 for s in right)


def test_search_tilts_first_if_it_went_out_at_the_top() -> None:
    top = steps(Search("person", "top", 0.0), tilt=20.0)
    assert top[0] == (0, 0, 0, 35, "search: tilt up to 35°, the person went out at the top")
    assert len(top) == 4 and all(s[3] is None for s in top[1:])
    at_limit = steps(Search("person", "top", 0.0), tilt=40.0)
    assert len(at_limit) == 3 and at_limit[0][3] is None  # already as high as it goes
    bottom = steps(Search("cat", "bottom", 0.0), tilt=0.0)
    assert bottom[0][3] == -15


def test_search_drives_towards_where_it_vanished() -> None:
    vanished = steps(Search("cat", "middle", 12.0))
    assert vanished[0] == (
        0,
        0,
        0,
        None,
        "search: looking again, the cat vanished in the middle of the picture",
    )
    assert vanished[1][1] > 0 and "to face where the cat was last seen" in vanished[1][4]
    assert (
        vanished[2][0] > 0
        and vanished[2][4] == "search: forward about 30 cm towards where the cat was last seen"
    )
    assert len(vanished) == 6  # then three turns
    # Close to something ahead, it goes no closer than the obstacle distance and a margin.
    settings = CarSettings()
    near = steps(Search("cat", "middle", 0.0), cm=settings.obst_cm + 10 + 12)
    assert "forward about 12 cm" in near[1][4]
    blocked = steps(Search("cat", "middle", 0.0), cm=settings.obst_cm + 12)
    assert all(s[0] == 0 for s in blocked)
    off_to_the_side = steps(Search("cat", "middle", 35.0))
    assert all(s[0] == 0 for s in off_to_the_side)  # too far round to drive towards
    # It was close: it has gone past to a side, so it looks again and turns towards where
    # it was, without driving.
    close = steps(Search("cat", "middle", 12.0, far=False))
    assert [s[0] for s in close] == [0, 0, 0, 0] and all(s[1] > 0 for s in close[1:])


def test_a_search_for_a_person_looks_at_head_height() -> None:
    scan = steps(Search("person", "scan"), tilt=0.0)
    assert scan[0][3] == 30 and all(s[3] is None for s in scan[1:])
    assert scan[0][4] == "search: turn 40° right, looking for the person (40° of 120°)"
    assert steps(Search("cat", "scan"), tilt=0.0)[0][3] is None


# -------------------------------------------------------------------- pictures


def test_boxes_are_drawn_on_a_bigger_picture() -> None:
    frame = jpeg(scene((LEFT, RED, BLUE)))
    out = annotate(frame, [(LEFT, "1 person", YELLOW), (RIGHT, "2 following", GREEN)])
    assert jpeg_size(out) == (640, 480)
    drawn = open_picture(out)
    r, g, _ = drawn.getpixel((round(RIGHT[0] * 640) + 1, round(0.5 * 480)))
    assert g > 150 and r < 120  # the green outline
    assert jpeg_size(annotate(frame, [], min_width=320)) == (320, 240)


def test_the_close_up_is_of_the_box() -> None:
    frame = jpeg(scene((LEFT, RED, BLUE)))
    close = crop(frame, LEFT, height=200)
    width, height = jpeg_size(close)
    assert height == 200 and 60 < width < 120
    centre = open_picture(close).getpixel((width // 2, 50))
    assert centre[0] > 150 and centre[2] < 90  # the red top
