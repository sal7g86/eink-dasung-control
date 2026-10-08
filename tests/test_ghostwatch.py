"""Ghost estimator tests with synthetic frames; no screen and no hardware."""

import json
import random

import pytest

from dasungctl.ghostwatch import (
    LIGHT_CONTENT,
    CaptureError,
    Frame,
    GhostElement,
    GhostModel,
    GhostResult,
    GhostWatcher,
    format_age,
    load_ghost_state,
    save_ghost_state,
)
from dasungctl.windows import Zone


def frame(width, height, value=255, *rects):
    """Grayscale frame: background value plus (x0, y0, x1, y1, shade) fills."""

    data = bytearray([value]) * (width * height)
    for x0, y0, x1, y1, shade in rects:
        for y in range(y0, y1):
            row = y * width
            for x in range(x0, x1):
                data[row + x] = shade
    return bytes(data)


def make_model(**kwargs):
    options = {
        "columns": 4,
        "min_cell_pixels": 8,
        "min_cell_fraction": 0.0,
    }
    options.update(kwargs)
    return GhostModel(32, 32, 32, 32, **options)


def erase_a_box(model):
    """Draw then erase a dark box; return the erase sample's result."""

    model.sample(frame(32, 32), now=100.0, wall=100.0)
    model.sample(frame(32, 32, 255, (0, 0, 16, 16, 0)), now=101.0, wall=101.0)
    return model.sample(frame(32, 32), now=102.0, wall=102.0)


def test_first_sample_starts_clean():
    model = make_model()

    result = model.sample(frame(32, 32), now=100.0, wall=100.0)

    assert result.level == 0
    assert result.elements == ()
    assert result.changed_pixels == 0
    assert result.samples == 1
    assert result.source_width == 32


def test_drawing_dark_content_leaves_a_light_ghost():
    model = make_model()
    model.sample(frame(32, 32), now=100.0, wall=100.0)

    result = model.sample(
        frame(32, 32, 255, (0, 0, 16, 16, 0)), now=101.0, wall=101.0
    )

    assert result.changed_pixels == 256
    assert result.level == 20
    assert result.light_level == 20
    assert len(result.elements) == 1
    element = result.elements[0]
    assert (element.x, element.y, element.width, element.height) == (0, 0, 16, 16)
    assert element.dark is False
    assert element.severity == 20
    assert result.dirty_fraction == 0.25


def test_adjacent_dark_and_light_areas_stay_separate():
    model = GhostModel(
        64, 64, 64, 64, columns=8, min_cell_pixels=8, min_cell_fraction=0.0
    )
    # Left dark on right light, then the exact inverse: the left half leaves
    # a dark ghost, the right half a light one, and the two must not merge.
    model.sample(frame(64, 64, 255, (0, 0, 32, 64, 0)), now=0.0, wall=0.0)

    result = model.sample(
        frame(64, 64, 0, (0, 0, 32, 64, 255)), now=1.0, wall=1.0
    )

    assert len(result.elements) == 2
    left, right = result.elements
    assert (left.x, left.y, left.width, left.dark) == (0, 0, 32, True)
    assert (right.x, right.y, right.width, right.dark) == (32, 0, 32, False)
    assert left.severity == 40
    assert right.severity == 20


def test_small_light_residue_is_not_a_ghost():
    model = make_model()
    model.sample(
        frame(32, 32, 255, (0, 0, 16, 16, 140)), now=0.0, wall=0.0
    )

    # 140 -> 120 darkens by 20, but the incomplete ink leaves only +2.
    result = model.sample(
        frame(32, 32, 255, (0, 0, 16, 16, 120)), now=1.0, wall=1.0
    )

    assert result.level == 0
    assert result.light_level == 0
    assert result.elements == ()


def test_preview_tints_light_residue():
    model = make_model()
    model.sample(frame(32, 32), now=0.0, wall=0.0)
    model.sample(frame(32, 32, 255, (0, 0, 16, 16, 0)), now=1.0, wall=1.0)

    rgb = model.preview_rgb(32, 32)

    inside = rgb[(5 * 32 + 5) * 3 : (5 * 32 + 5) * 3 + 3]
    outside = rgb[(5 * 32 + 30) * 3 : (5 * 32 + 30) * 3 + 3]
    assert inside[0] > inside[2]  # a warm tint, not a grey
    assert tuple(outside) == (235, 235, 235)


def test_erased_dark_box_leaves_a_dark_ghost():
    model = make_model()
    result = erase_a_box(model)

    assert result.level == 36
    assert len(result.elements) == 1
    element = result.elements[0]
    assert (element.x, element.y, element.width, element.height) == (0, 0, 16, 16)
    assert element.dark is True
    assert element.severity == 36
    assert result.dirty_fraction == 0.25


def test_unchanged_frames_keep_the_ghost_and_grow_its_age():
    model = make_model()
    erase_a_box(model)

    result = model.sample(frame(32, 32), now=110.0, wall=110.0)

    assert result.changed_pixels == 0
    assert result.level == 36
    assert result.elements[0].age == 8.0


def test_two_separated_boxes_become_two_elements():
    model = GhostModel(
        64, 64, 64, 64, columns=8, min_cell_pixels=8, min_cell_fraction=0.0
    )
    model.sample(frame(64, 64), now=0.0, wall=0.0)
    model.sample(
        frame(
            64,
            64,
            255,
            (0, 0, 16, 16, 0),
            (32, 32, 48, 48, 0),
        ),
        now=1.0,
        wall=1.0,
    )

    result = model.sample(frame(64, 64), now=2.0, wall=2.0)

    assert len(result.elements) == 2
    boxes = [
        (element.x, element.y, element.width, element.height)
        for element in result.elements
    ]
    assert boxes == [(0, 0, 16, 16), (32, 32, 16, 16)]
    # The element list is ordered by position, top-left first.
    assert result.elements[0].y < result.elements[1].y


def test_tiny_changes_do_not_become_ghosts():
    model = make_model()
    model.sample(frame(32, 32), now=0.0, wall=0.0)
    model.sample(frame(32, 32, 255, (0, 0, 2, 2, 0)), now=1.0, wall=1.0)

    result = model.sample(frame(32, 32), now=2.0, wall=2.0)

    assert result.level == 0
    assert result.elements == ()


def test_reset_clears_the_estimate():
    model = make_model()
    erase_a_box(model)

    result = model.reset(now=103.0, wall=103.0)

    assert result.level == 0
    assert result.elements == ()
    # A later frame equal to the last content keeps the estimate clean.
    assert model.sample(frame(32, 32), now=104.0, wall=104.0).level == 0


def test_reset_area_clears_only_the_named_rectangle():
    model = make_model()
    model.sample(frame(32, 32), now=0.0, wall=0.0)
    model.sample(
        frame(32, 32, 255, (0, 0, 8, 8, 0), (24, 24, 32, 32, 0)),
        now=1.0,
        wall=1.0,
    )
    erased = model.sample(frame(32, 32), now=2.0, wall=2.0)

    assert len(erased.elements) == 2

    result = model.reset_area(0, 0, 8, 8, now=3.0, wall=3.0)

    assert len(result.elements) == 1
    remaining = result.elements[0]
    assert (remaining.x, remaining.y) == (24, 24)
    assert (remaining.width, remaining.height) == (8, 8)
    assert result.changed_pixels == 0


def recomputed_stats(model):
    """Per-cell statistics rebuilt from the pixels, the slow way."""

    cells = model.columns * model.rows
    counts, sums = [0] * cells, [0] * cells
    light_counts, light_sums = [0] * cells, [0] * cells
    for index in range(model.width * model.height):
        row, column = divmod(index, model.width)
        cell = (row // model.cell_width) * model.columns + (
            column // model.cell_width
        )
        level = model.error[index]
        value = model._previous[index]
        if level <= -model.min_error and value >= LIGHT_CONTENT:
            counts[cell] += 1
            sums[cell] -= level
        elif level >= model.min_error and value < LIGHT_CONTENT:
            light_counts[cell] += 1
            light_sums[cell] += level
    return counts, sums, light_counts, light_sums


def test_incremental_statistics_match_a_full_recount():
    rng = random.Random(11)
    for width, height, scale in ((32, 32, 1), (45, 29, 3)):
        model = GhostModel(
            width,
            height,
            width * scale,
            height * scale,
            columns=5,
            min_cell_pixels=4,
            min_cell_fraction=0.0,
        )
        data = bytearray([255]) * (width * height)
        for step in range(60):
            for _ in range(rng.randint(0, 4)):
                x0, y0 = rng.randrange(width), rng.randrange(height)
                x1 = min(width, x0 + rng.randint(1, 12))
                y1 = min(height, y0 + rng.randint(1, 12))
                shade = rng.choice((0, 60, 127, 128, 200, 255))
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        jitter = rng.randint(-14, 14)
                        data[y * width + x] = max(0, min(255, shade + jitter))
            choice = rng.random()
            if choice < 0.05:
                model.reset(now=float(step), wall=float(step))
            elif choice < 0.2 and model.result is not None:
                model.reset_area(
                    rng.randrange(width * scale),
                    rng.randrange(height * scale),
                    rng.randint(1, 20 * scale),
                    rng.randint(1, 20 * scale),
                    now=float(step),
                    wall=float(step),
                )
            else:
                model.sample(bytes(data), now=float(step), wall=float(step))
            assert (
                model._counts,
                model._sums,
                model._light_counts,
                model._light_sums,
            ) == recomputed_stats(model)
            for index in range(width * height):
                assert model.error[index] == (
                    model._shown[index] - model._previous[index]
                )


def test_flashing_an_element_clears_it_on_a_scaled_screen():
    # 48x36 model pixels for a 2200x1650 screen (45.8 screen pixels each):
    # the element rectangle must map back onto exactly its model pixels.
    model = GhostModel(
        48, 36, 2200, 1650, columns=4, min_cell_pixels=4, min_cell_fraction=0.0
    )
    model.sample(frame(48, 36), now=0.0, wall=0.0)
    model.sample(frame(48, 36, 255, (3, 4, 9, 11, 0)), now=1.0, wall=1.0)
    erased = model.sample(frame(48, 36), now=2.0, wall=2.0)
    (element,) = erased.elements

    cleared = model.reset_area(
        element.x, element.y, element.width, element.height, now=3.0, wall=3.0
    )

    assert cleared.elements == ()
    assert cleared.level == 0
    assert model.sample(frame(48, 36), now=4.0, wall=4.0).elements == ()


def test_elements_cover_the_residue_not_whole_cells():
    model = make_model()  # 8-pixel cells
    model.sample(frame(32, 32), now=0.0, wall=0.0)
    model.sample(frame(32, 32, 255, (2, 3, 6, 7, 0)), now=1.0, wall=1.0)

    (element,) = model.sample(frame(32, 32), now=2.0, wall=2.0).elements

    assert (element.x, element.y, element.width, element.height) == (2, 3, 4, 4)


def test_content_drawn_during_a_flash_is_clean_afterwards():
    model = make_model()
    model.sample(frame(32, 32), now=0.0, wall=0.0)
    model.sample(frame(32, 32, 255, (0, 0, 8, 8, 0)), now=1.0, wall=1.0)
    model.sample(frame(32, 32), now=2.0, wall=2.0)

    model.reset_area(0, 0, 8, 8, now=3.0, wall=3.0)
    # A dark box appears under the overlay before the next sample: the
    # flash ended on it, so it is not a fresh light ghost.
    after = model.sample(
        frame(32, 32, 255, (0, 0, 8, 8, 0)), now=4.0, wall=4.0
    )

    assert after.elements == ()
    # Outside a flashed area the same drawing still leaves its residue.
    later = model.sample(
        frame(32, 32, 255, (0, 0, 8, 8, 0), (16, 16, 24, 24, 0)),
        now=5.0,
        wall=5.0,
    )
    assert [(item.x, item.y) for item in later.elements] == [(16, 16)]


def test_changes_within_the_noise_keep_the_error_and_follow_the_content():
    model = make_model()
    model.sample(frame(32, 32), now=0.0, wall=0.0)
    model.sample(frame(32, 32, 255, (0, 0, 16, 16, 0)), now=1.0, wall=1.0)
    before = model.sample(frame(32, 32), now=2.0, wall=2.0)
    level = model.error[0]
    assert level < 0

    # A slow fade in steps below the noise: no new residue, same error.
    for step, shade in enumerate((250, 245, 240, 235, 230), start=3):
        model.sample(frame(32, 32, shade), now=float(step), wall=float(step))
        assert model.error[0] == level
        assert model._shown[0] == shade + level

    after = model.sample(frame(32, 32, 230), now=9.0, wall=9.0)
    assert after.level == before.level


def test_version_moves_only_when_the_estimate_can_change():
    model = make_model()
    model.sample(frame(32, 32), now=0.0, wall=0.0)
    start = model.version

    model.sample(frame(32, 32), now=1.0, wall=1.0)
    assert model.version == start

    model.sample(frame(32, 32, 255, (0, 0, 4, 4, 0)), now=2.0, wall=2.0)
    assert model.version > start
    moved = model.version
    model.reset_area(0, 0, 4, 4, now=3.0, wall=3.0)
    assert model.version > moved


def test_reset_area_requires_a_positive_rectangle():
    model = make_model()
    with pytest.raises(ValueError):
        model.reset_area(0, 0, 0, 8, now=1.0, wall=1.0)


def test_repeated_draw_and_erase_keeps_the_ghost_high():
    """The model must not pretend that flipping content cleans the panel:

    only a rewrite toward the target or a refresh reduces the residue, and
    the residue after an erase depends on how dark the ink had become.
    """

    model = make_model()
    first = erase_a_box(model)
    model.sample(frame(32, 32, 255, (0, 0, 16, 16, 0)), now=103.0, wall=103.0)
    second = model.sample(frame(32, 32), now=104.0, wall=104.0)

    assert first.level == 36
    assert second.level >= first.level


def test_frame_size_and_options_are_validated():
    model = make_model()
    with pytest.raises(ValueError):
        model.sample(b"\x00" * 10, now=0.0)
    with pytest.raises(ValueError):
        GhostModel(32, 32, 32, 32, gamma_ink=0)
    with pytest.raises(ValueError):
        GhostModel(32, 32, 32, 32, gamma_erase=101)
    with pytest.raises(ValueError):
        GhostModel(32, 32, 32, 32, noise=256)
    with pytest.raises(ValueError):
        GhostModel(32, 32, 32, 32, min_error=0)


def test_preview_shows_the_ghost_and_its_box():
    model = make_model()
    erase_a_box(model)

    rgb = model.preview_rgb(32, 32)

    assert len(rgb) == 32 * 32 * 3
    inside = rgb[(5 * 32 + 5) * 3]
    outside = rgb[(5 * 32 + 30) * 3]
    assert inside < outside
    # The element box is outlined in the accent colour.
    box = rgb[(0 * 32 + 16) * 3 : (0 * 32 + 16) * 3 + 3]
    assert tuple(box) == (30, 90, 220)
    with pytest.raises(ValueError):
        model.preview_rgb(0, 10)


def test_state_round_trip(tmp_path):
    model = make_model()
    result = erase_a_box(model)
    path = tmp_path / "ghost.json"

    save_ghost_state(result, path, notified_at=123.0, armed=False)
    state = load_ghost_state(path)

    assert state is not None
    assert state.level == result.level
    assert state.notified_at == 123.0
    assert state.armed is False
    assert len(state.elements) == 1
    assert state.elements[0] == result.elements[0]


def test_corrupt_state_is_rejected(tmp_path):
    path = tmp_path / "ghost.json"
    path.write_text(json.dumps({"level": 5, "surprise": True}))
    with pytest.raises(ValueError):
        load_ghost_state(path)
    path.write_text("{not json")
    with pytest.raises(ValueError):
        load_ghost_state(path)
    assert load_ghost_state(tmp_path / "absent.json") is None


def annotate_left(x, y):
    return ("konsole", True) if x < 16 else None


def test_element_label_is_captured_when_the_ghost_forms():
    model = make_model()
    model.sample(frame(32, 32), now=0.0, wall=0.0, annotate=annotate_left)
    model.sample(
        frame(32, 32, 255, (0, 0, 16, 16, 0)),
        now=1.0,
        wall=1.0,
        annotate=annotate_left,
    )

    result = model.sample(
        frame(32, 32), now=2.0, wall=2.0, annotate=annotate_left
    )
    assert result.elements[0].app == "konsole"
    assert result.elements[0].fullscreen is True

    # The label survives samples with no changes.
    later = model.sample(
        frame(32, 32), now=3.0, wall=3.0, annotate=annotate_left
    )
    assert later.elements[0].app == "konsole"

    # A new ghost on the other side is annotated on its own.
    model.reset(now=4.0, wall=4.0)
    model.sample(
        frame(32, 32, 255, (16, 16, 32, 32, 0)),
        now=5.0,
        wall=5.0,
        annotate=annotate_left,
    )
    result = model.sample(
        frame(32, 32), now=6.0, wall=6.0, annotate=annotate_left
    )
    assert result.elements[0].app is None


def test_state_round_trip_keeps_labels(tmp_path):
    model = make_model()
    model.sample(frame(32, 32), now=0.0, wall=0.0, annotate=annotate_left)
    model.sample(
        frame(32, 32, 255, (0, 0, 16, 16, 0)),
        now=1.0,
        wall=1.0,
        annotate=annotate_left,
    )
    result = model.sample(
        frame(32, 32), now=2.0, wall=2.0, annotate=annotate_left
    )
    path = tmp_path / "ghost.json"

    save_ghost_state(result, path)
    state = load_ghost_state(path)

    assert state is not None
    assert state.elements[0].app == "konsole"
    assert state.elements[0].fullscreen is True


def test_state_round_trip_keeps_light_polarity(tmp_path):
    model = make_model()
    model.sample(frame(32, 32), now=0.0, wall=0.0)
    result = model.sample(
        frame(32, 32, 255, (0, 0, 16, 16, 0)), now=1.0, wall=1.0
    )
    path = tmp_path / "ghost.json"

    save_ghost_state(result, path)
    state = load_ghost_state(path)

    assert state is not None
    assert len(state.elements) == 1
    assert state.elements[0].dark is False
    assert state.elements[0] == result.elements[0]


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeCapturer:
    """Serves a queue of frames, repeating the last one."""

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.grabs = 0
        self.resets = 0
        self.source_origin = (0, 0)

    def grab(self, width):
        self.grabs += 1
        if not self.frames:
            raise CaptureError("no frames configured")
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]

    def reset(self):
        self.resets += 1


def ghost_frames():
    """White, dark box, white again: one erased 64x64 box on a 128x128 frame."""

    return [
        Frame(frame(128, 128), 128, 128, 128, 128),
        Frame(frame(128, 128, 255, (0, 0, 64, 64, 0)), 128, 128, 128, 128),
        Frame(frame(128, 128), 128, 128, 128, 128),
    ]


def make_watcher(capturer, clock, **kwargs):
    options = {"interval": 2.0, "threshold": 20.0, "width": 128}
    options.update(kwargs)
    return GhostWatcher(capturer, clock=clock, wall_clock=clock, **options)


def test_watcher_samples_on_its_interval():
    clock = FakeClock()
    watcher = make_watcher(
        FakeCapturer(ghost_frames()), clock, max_interval=2.0
    )

    assert watcher.due() is True
    watcher.sample()
    assert watcher.due() is False
    clock.advance(2.0)
    assert watcher.due() is True


def test_watcher_disabled_never_samples():
    clock = FakeClock()
    watcher = make_watcher(FakeCapturer(ghost_frames()), clock, enabled=False)

    assert watcher.due() is False
    assert watcher.sample() is None


def test_watcher_paused_never_samples_and_resumes():
    clock = FakeClock()
    capturer = FakeCapturer(ghost_frames())
    watcher = make_watcher(capturer, clock)

    watcher.sample()
    assert capturer.grabs == 1

    watcher.set_paused(True)
    clock.advance(5.0)
    assert watcher.due() is False
    assert watcher.sample() is None
    assert capturer.grabs == 1

    watcher.set_paused(False)
    assert watcher.due() is True
    assert watcher.sample() is not None
    assert capturer.grabs == 2


def test_watcher_keeps_capture_failures_and_retries():
    clock = FakeClock()
    capturer = FakeCapturer()
    watcher = make_watcher(capturer, clock)

    assert watcher.sample() is None
    assert watcher.capture_failed is True
    assert watcher.error is not None
    assert watcher.due() is False

    watcher.retry()

    assert watcher.capture_failed is False
    assert watcher.error is None
    assert capturer.resets == 1
    assert watcher.due() is True


def test_watcher_rebuilds_the_model_when_the_size_changes():
    clock = FakeClock()
    capturer = FakeCapturer(
        [
            Frame(frame(128, 128), 128, 128, 128, 128),
            Frame(frame(96, 64), 96, 64, 256, 128),
        ]
    )
    watcher = make_watcher(capturer, clock)

    watcher.sample()
    watcher.sample()

    assert watcher.model is not None
    assert (watcher.model.width, watcher.model.height) == (96, 64)
    assert watcher.model.source_width == 256


def test_watcher_alerts_once_and_rearms_after_the_level_drops():
    clock = FakeClock()
    # A box on ~15% of the panel: erasing it gives a dark level just above
    # the threshold, drawing over it a light residue just below the re-arm
    # fraction (60% of the threshold).
    box = (0, 0, 49, 49, 0)
    frames = [
        Frame(frame(128, 128), 128, 128, 128, 128),
        Frame(frame(128, 128, 255, box), 128, 128, 128, 128),
        Frame(frame(128, 128), 128, 128, 128, 128),
    ]
    watcher = make_watcher(FakeCapturer(frames), clock)
    for _step in range(3):
        watcher.sample()

    alert = watcher.maybe_alert()
    assert alert is not None
    assert "1 area" in alert
    assert watcher.maybe_alert() is None

    # Draw dark content over the ghost and erase it again: the light residue
    # drops the level below the re-arm fraction, so the alert arms itself;
    # the returning dark ghost is then held back only by the cooldown.
    frame_dark = Frame(frame(128, 128, 255, box), 128, 128, 128, 128)
    frame_light = Frame(frame(128, 128), 128, 128, 128, 128)
    watcher.capturer = FakeCapturer([frame_dark, frame_light])
    clock.advance(3.0)
    watcher.sample()
    watcher.maybe_alert()
    clock.advance(3.0)
    watcher.sample()

    # Inside the cooldown window no second alert is reported.
    assert watcher.maybe_alert() is None
    clock.advance(1000.0)
    assert watcher.maybe_alert() is not None


def test_watcher_reset_rearms_immediately():
    clock = FakeClock()
    watcher = make_watcher(FakeCapturer(ghost_frames()), clock)
    for _step in range(3):
        watcher.sample()
    assert watcher.maybe_alert() is not None

    watcher.reset()
    watcher.capturer = FakeCapturer(
        [
            Frame(frame(128, 128, 255, (0, 0, 64, 64, 0)), 128, 128, 128, 128),
            Frame(frame(128, 128), 128, 128, 128, 128),
        ]
    )
    clock.advance(3.0)
    watcher.sample()
    clock.advance(3.0)
    watcher.sample()

    assert watcher.maybe_alert() is not None


def test_watcher_backs_off_while_nothing_changes():
    clock = FakeClock()
    watcher = make_watcher(
        FakeCapturer(ghost_frames()[:1]), clock, max_interval=8.0
    )

    watcher.sample()
    assert watcher.current_interval == 4.0
    clock.advance(3.0)
    assert watcher.due() is False
    clock.advance(1.0)
    assert watcher.due() is True

    watcher.sample()
    assert watcher.current_interval == 8.0
    clock.advance(7.0)
    assert watcher.due() is False
    clock.advance(1.0)
    assert watcher.due() is True


class TrackingCapturer(FakeCapturer):
    """A capture that reports changes, like X11 damage or Wayland frames."""

    def __init__(self, frames=()):
        super().__init__(frames)
        self.changed = False
        self.full_requests = 0

    def poll_changes(self):
        return self.changed

    def grab(self, width):
        self.changed = False
        return super().grab(width)

    def request_full(self):
        self.full_requests += 1


def test_watcher_with_change_reports_samples_only_after_changes():
    clock = FakeClock()
    capturer = TrackingCapturer(ghost_frames()[:1])
    watcher = make_watcher(capturer, clock, max_interval=30.0)

    assert watcher.due() is True  # the first sample is always due
    watcher.sample()
    clock.advance(10.0)
    assert watcher.due() is False  # a still screen costs nothing

    capturer.changed = True
    assert watcher.due() is True
    watcher.sample()
    capturer.changed = True
    clock.advance(1.0)
    assert watcher.due() is False  # changes wait for the base interval
    clock.advance(1.0)
    assert watcher.due() is True


def test_watcher_with_change_reports_runs_a_full_heartbeat():
    clock = FakeClock()
    capturer = TrackingCapturer(ghost_frames()[:1])
    watcher = make_watcher(capturer, clock, max_interval=30.0)
    watcher.sample()

    clock.advance(29.0)
    assert watcher.due() is False
    clock.advance(1.0)
    assert watcher.due() is True
    watcher.sample()

    assert capturer.full_requests == 1
    clock.advance(2.0)
    assert watcher.due() is False


def test_watcher_keeps_the_base_cadence_while_the_window_is_open():
    clock = FakeClock()
    capturer = TrackingCapturer(ghost_frames()[:1])
    watcher = make_watcher(capturer, clock)
    watcher.sample()
    watcher.set_force_base(True)

    clock.advance(2.0)

    assert watcher.due() is True


def test_watcher_requested_sample_skips_the_wait():
    clock = FakeClock()
    capturer = TrackingCapturer(ghost_frames()[:1])
    watcher = make_watcher(capturer, clock)
    watcher.sample()

    watcher.request_sample()

    assert watcher.due() is True
    watcher.sample()
    assert watcher.due() is False


def test_watcher_without_change_reports_keeps_the_timer():
    clock = FakeClock()
    capturer = TrackingCapturer(ghost_frames()[:1])
    capturer.poll_changes = lambda: None  # the capture cannot tell
    watcher = make_watcher(capturer, clock, max_interval=8.0)
    watcher.sample()

    clock.advance(4.0)

    assert watcher.due() is True


def test_watcher_reset_area_keeps_the_other_areas():
    clock = FakeClock()
    frames = [
        Frame(frame(128, 128), 128, 128, 128, 128),
        Frame(
            frame(128, 128, 255, (0, 0, 32, 32, 0), (64, 64, 96, 96, 0)),
            128,
            128,
            128,
            128,
        ),
        Frame(frame(128, 128), 128, 128, 128, 128),
    ]
    watcher = make_watcher(FakeCapturer(frames), clock)
    for _step in range(3):
        watcher.sample()
    assert len(watcher.result.elements) == 2

    watcher.reset_area(0, 0, 32, 32)

    assert len(watcher.result.elements) == 1
    remaining = watcher.result.elements[0]
    assert (remaining.x, remaining.y) == (64, 64)


def test_watcher_returns_to_base_when_content_changes():
    clock = FakeClock()
    watcher = make_watcher(FakeCapturer(ghost_frames()), clock, max_interval=8.0)

    watcher.sample()
    assert watcher.current_interval == 4.0

    clock.advance(4.0)
    watcher.sample()

    assert watcher.current_interval == 2.0


def test_watcher_force_base_overrides_the_backoff():
    clock = FakeClock()
    watcher = make_watcher(FakeCapturer(ghost_frames()[:1]), clock)

    watcher.sample()
    assert watcher.current_interval == 4.0

    watcher.set_force_base(True)
    assert watcher.current_interval == 2.0
    watcher.set_force_base(False)
    assert watcher.current_interval == 4.0


class FakeZones:
    def __init__(self, zones=()):
        self._zones = tuple(zones)
        self.calls = []

    def zones(self, origin_x, origin_y, width, height):
        self.calls.append((origin_x, origin_y, width, height))
        return self._zones


def test_watcher_labels_elements_from_the_zone_provider():
    clock = FakeClock()
    provider = FakeZones([Zone("konsole", 0, 0, 128, 128, True)])
    watcher = make_watcher(
        FakeCapturer(ghost_frames()), clock, zones=provider
    )
    for _step in range(3):
        watcher.sample()

    assert provider.calls
    assert provider.calls[0] == (0, 0, 128, 128)
    element = watcher.result.elements[0]
    assert element.app == "konsole"
    assert element.fullscreen is True

    alert = watcher.maybe_alert()
    assert alert is not None
    assert "(konsole)" in alert


def test_watcher_state_survives_a_restart(tmp_path):
    clock = FakeClock()
    path = tmp_path / "ghost.json"
    watcher = make_watcher(FakeCapturer(ghost_frames()), clock)
    for _step in range(3):
        watcher.sample()
    watcher.maybe_alert()
    watcher.save_state(path, force=True)

    restarted = make_watcher(FakeCapturer(ghost_frames()), FakeClock())
    state = restarted.load_state(path)

    assert state is not None
    assert state.armed is False
    assert len(state.elements) == 1


def test_watcher_survives_a_corrupt_state_file(tmp_path):
    path = tmp_path / "ghost.json"
    path.write_text("{not json")
    watcher = make_watcher(FakeCapturer(ghost_frames()), FakeClock())

    assert watcher.load_state(path) is None
    assert watcher.error is not None


def test_format_age_scales():
    assert format_age(30) == "30 s"
    assert format_age(120) == "2 min"
    assert format_age(7200) == "2 h"


def test_ghost_element_is_frozen():
    element = GhostElement(1, 2, 3, 4, 5, True, 6.0)
    with pytest.raises(Exception):
        element.x = 9
    result = GhostResult(0, 0, (), 0.0, 0, 0, 0.0, 1, 1, 1, 1)
    with pytest.raises(Exception):
        result.level = 1
