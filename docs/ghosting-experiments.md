# Ghosting experiments (archive)

This is the English summary of the project's ghosting lab notes (2026-09-25
to 2026-10-01). It documents the physical measurements, the rejected
approaches and the reasoning behind the current estimate-driven zone
clearing ([ghost-estimate.md](ghost-estimate.md)). The tools cited in the
older procedures (`zone_calibrate`, `ghost_test`, the TUI) were removed on
2026-09-26 and are not available in this repository; the photo analysis tool
`tools/ghost_photo.py` still ships.

## Purpose

Measure whether the soft and hard refresh reduce ghosting, how long the
effect lasts, and how often a clear is needed in normal use. The results led
to the grid zone-clearing mode, which was later removed and replaced.

## Setup

Keep fixed for a whole trial: model and firmware (Paperlike HD Revolutionary
13.3, protocol `0x30`), display mode, contrast, speed, frontlight,
temperature, room light and monitor position, and no auto-refresh timer.

Ghosting was produced with a high-contrast `pattern` and judged on a uniform
`canvas`; the clear was the tray's `Refresh` submenu or the `Controls…`
buttons. Photos of the panel were taken with a fixed phone position,
distance, zoom, exposure and crop; the crop had to be identical for every
photo because `ghost_index` is only comparable at equal crop. The visual
description ("clean, no shadows", "vertical lines in the center", ...) was
recorded together with the objective photo statistics; the optional 0-5
scale remained secondary.

## Phase 1: soft vs hard refresh

Three repetitions per type in alternating order; for each repetition: pattern
3 s, canvas 5 s, photo A, refresh, canvas 5 s, photo B. A clear is
"effective" when the description becomes "clean" and `ghost_index` drops by
at least 50%.

Pilot results (2026-09-25):

| # | type | description A | description B | ghost_index A | ghost_index B |
|---|------|---------------|---------------|---------------|---------------|
| 1 | soft | box + visible horizontal lines | box gone, no lines | 11.6 | 8.9 |
| 2 | hard | box + lines + crosses | clean screen, no ghost | 10.3 | 8.9 |

Both clears removed the ghost; the hard refresh was **not** better than the
soft one (11.6 → 8.9 soft, 10.3 → 8.9 hard). The 8.9 floor is illumination and
JPEG noise: the real A-B difference is about 1.1% (RMSE 0.0114 soft, 0.0110
hard), and clean photos from two sessions differed by 0.015, so smaller
differences are not significant without repetitions.

## Phase 2: accumulation in normal use

A 30-60 minute real work session with no automatic clear: an activity script
ran in the background, and every 5 minutes a canvas was shown for 5 seconds
and photographed, recording the visual state, `ghost_index`, changed pixels
per minute, and any manual clear. The goal was the time to the first visible
ghost, the time to a marked ghost, and the correlation with changed
pixels/minute.

Safety limits for the whole trial: at most ~10 soft and 3 hard refreshes,
every frame annotated; on anomalous behaviour (lock-up, strange flicker) stop
and power-cycle as rollback. The hard frame had never been sent on this unit
before, so the first sends were one at a time.

## Decision: smart global clear rejected

After the pilot the plan was a "smart global clear" (a soft clear when the
user stops and the accumulated changed pixels pass a threshold, with minimum
and maximum intervals; soft by default because hard was not better). It was
implemented and then **rejected on request**: automatic refresh had to stay
**zone-only, never global**.

The replacement was a grid of zones (default 6x8 cells of ~183 px): each zone
accumulated changed pixels (with a `min_frame_pixels` noise filter) and was
flashed when it passed `zone_fill` times its area and had been still for
`settle_ms`; `force_fill` cleaned it without a pause, and `cooldown_ms` /
`cycle_cooldown_ms` limited the frequency. The flash was `auto` (the changed
bounding box above `flash_min_px`, the whole zone above `flash_full_at`) and
used `single`, `white-black`, `white`, `black` or repeated pulses. No serial
frame was ever automatic: global clears stayed manual. Defaults lived in the
removed `zones` config section (`zone_fill` 4.0, `force_fill` 12.0,
`flash_full_at` 0.5, `flash_min_px` 4096, `min_frame_pixels` 8, `flash_style`
single, `flash_level` 96, `settle_ms` 300, `cooldown_ms` 15000, ...).

## What was verified in the grid mode

- The extreme-frame fallback was exact: 15 out of 15 intervals matched an
  independent count, with no overestimates.
- With `top` in Konsole, releases happened only in the terminal zones.
- A moderate ghost (3x4 s pattern) was reduced but not removed by one soft
  96/60 pulse; three pulses cleaned it.
- On an area stressed with ~25 test flashes, soft and white-black left the
  same "hint", which a single global soft refresh removed: it was a local
  residue, not a limit of the grey.
- An observed "outline" came from the `--max-rects 4` limit used in that test,
  not from the algorithm.

Unresolved: `flash_repeat` 3 was rejected because the burst flickers; one
soft pulse can leave a residue on heavier ghosts. Possible next steps were a
stronger single pulse (duration or level), a mixed strategy (routine single +
occasional white-black), or a real regional refresh if the protocol ever
reveals one.

## Adaptive mode

A second software mode, `adaptive`, was implemented and remained
experimental; the maintainer never found it convincing (zones too small,
ghosting persisting). It accepted larger blocks up to 25% of the output and
successive zones during pauses, with at least 3 seconds between zones;
repeated bursts on the same zone, automatic global refresh and a permanent
camera stayed excluded.

Two defects were found and fixed but did not settle the efficacy question:

- **TUI crash** (2026-09-25): creating the overlay failed with `TypeError:
  Missing required argument flags`; the Xlib `set_wm_hints` call needed
  `flags=Xutil.InputHint`. Serialization errors become `OverlayError` and
  keep the TUI alive. Verified with 397 passing tests; no physical pulse
  during that verification.
- **"It never cleans and Xorg eats CPU"** (2026-09-26): the idle check only
  accepted screensaver state `0`, so on this host (`state=3`, X screensaver
  disabled) every poll stopped at `input-idle-unavailable` and the adaptive
  mode had **never** run an automatic pulse. The geometry check also called
  `RRGetScreenResources` on every poll, re-reading the EDIDs (~230 ms of X
  server per call, polled every 100-300 ms); it now uses
  `RRGetScreenResourcesCurrent` (~0.4 ms), with the full probe only at
  startup. After both fixes the reasons reached `ready` and Xorg stayed
  around 10% CPU in a 22 s observation.

A "balanced" tuning followed: `settle_ms` and `input_idle_ms` 700 → 500, a
new 10 s debt deadline (`max_pending_ms`) after which a 250 ms pause is
enough, `risk_threshold` 1.5 → 1.0, local `cooldown_ms` 60000 → 15000, one
block per cycle and 3 s between pulses. The base cycle was 64/150 ms; the
judgement on the panel remained open.

## Large-block cycle candidates (v2)

Blocks up to 25% of the output, at least 500 ms of quiet (250 ms past the
debt deadline), one cycle at a time, 3 s minimum interval, 15 s local
cooldown. Candidates (indices kept for the measurement sheets): 0 reference
single grey 96/60 ms; 1 reinforced single grey 64/150 ms; 2 RGB inversion
150 ms; 3 white 100 + black 100 ms; 4 white 300 + black 300 ms (added
2026-09-26); 5 RGB inversion 400 ms (added 2026-09-26). Candidates 4 and 5
tested the hypothesis that 150 ms is too short for the controller to finish
the transition.

A candidate had to beat the reference in all three repetitions of both a 10%
and a 25% stage, reduce the ghost versus its own "before", and be acceptable
in comfort; among the qualifying ones the winner had the lowest mean relative
residue. No winner meant `cycle: null`: the failure was documented and the
automatic mode kept the experimental 64/150 ms cycle. The comparison was
never run on the panel before the mode was removed.

## External references

- [Glider/Caster](https://github.com/Modos-Labs/Glider#hybrid-greyscale-mode):
  per-pixel updates and a more accurate rendering after stabilization; it
  needs its own controller, so only the timing criterion transfers, not the
  waveforms.
- [KOReader](https://github.com/koreader/koreader/blob/master/frontend/ui/uimanager.lua):
  fast regional, partial and flashing updates, with region limits to contain
  unwanted flashes; it needs driver support.
- A [Dasung experience report](https://www.reddit.com/r/eink/comments/1g5otwv/reviewexperience_dasung_paperlike_color/):
  briefly switching to a dark terminal cleans a light one; unmeasured and on
  another model.

No ready-made solution with proven superior regional efficacy on this monitor
was found, and none of the three sources proves that an overlay is equivalent
to a real regional refresh.

## Estimate-driven zone clearing (2026-10-01)

After the 2026-09-26 removal, the `Ghost estimate…` estimate was built. On
maintainer request, zone clearing was reintroduced with a different trigger:
not accumulated changed pixels per cell, but **the areas the estimator
reports** (position, severity, age).

- X11 overlay (borderless override-redirect window above the area) with the
  shipped default of a `single` grey pulse (168/30 ms) and automatic clearing
  on; the `white-black` style (15 ms per phase in the shipped defaults) and
  every level/duration are edited in `Ghost estimate…`'s `Clearing settings`, with
  `Test flash` to preview. Settings are saved in the tray state file.
- `Automatic clearing` switch (on by default, remembered) and a manual
  `Clear areas` button; after the flash the estimate for that area is reset.
- Deliberately minimal policy: areas are flashed once their age reaches
  `delay` (default 15 s), all the due ones together in one synchronized wave,
  even while the screen is in use, each area whole however large. No
  severity/cooldown/spacing parameters. Not available on Wayland.
- No serial frame: the global refresh stays manual.

**The physical efficacy still has to be judged on the panel.** The shipped
defaults are the maintainer's tuned values; the archive's candidates 4
(300/300 ms) and 5 (RGB inversion 400 ms) remain the calibration references,
and durations are edited from the estimate window.

## Rejected: continuous low-amplitude maintenance (2026-10-01)

The idea was to replace the flash with a nearly invisible periodic
perturbation that forces the panel to rewrite continuously. Tested with a
full-panel RGBA overlay toggled at 1 Hz:

- white alpha 2/4/8% → real content shift +1.0…+3.6 levels;
- black alpha 2/4% → −4.4…−8.7 levels (it affects light backgrounds more).

Outcomes (maintainer's judgement on the panel): the panel clearly reacts with
a light shimmer at every amplitude, and past the threshold the shimmer no
longer grows with amplitude, so invisibility cannot be dosed; 40 seconds per
setting did **not** clean a ghost on a light background (white removed
something only on dark areas, black left the light-background ghost
unchanged). Useful by-product: the panel reacts to ~1-level changes, well
below the model's noise floor (10), so the estimator never sees the
perturbation. Conclusion: an imperceptible continuous perturbation cannot
replace the flash; the short white-black flashes (the maintainer reports good
results at **20 ms per phase**, in both styles) stay, and low-amplitude
maintenance must not be re-proposed.

Side note: the test overlay was not click-through because of a PyGObject
signature error (`input_shape_combine_region(region, 0, 0)`); the same bug is
present in the tray flasher (`ZoneFlasher._pass_through`) and is still to be
fixed.
