# Development

## Repository layout

- `src/dasungctl/` — the package: `cli.py` (entry point), `tray.py` (GTK
  tray, controller, startup) and `tray_windows.py` (Controls and Ghost
  windows), `client.py`/`protocol.py`/`transport.py` (serial),
  `panels.py` (per-model tables), `controls.py`/`state.py`/`config.py`
  (settings and persistence),
  `ghostwatch.py`/`screencap.py`/`windows.py`/`zoneclear.py` (ghost
  estimate), `doctor.py`/`locking.py`/`paths.py`/`logfile.py`.
- `tests/` — offline tests only: fakes, recorded frames, no serial device and
  no hardware. `tests/conftest.py` points every XDG directory at a temporary
  tree.
- `tools/` — offline developer tools (`ghost_photo.py` measures ghosting on
  photos with ImageMagick); they must never import `dasungctl.transport` or
  touch serial/USB.
- `docs/` — the project documentation; the repository root keeps only
  `README.md`.

## Environment and commands

```console
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

python -m pytest -q                 # seconds, never touches hardware
python -m pytest tests/test_tray.py -q
uvx ruff check src tests tools --select F,E9
```

There is no formatter or type checker configured; only ruff `F,E9` is
expected to pass, so match the style of the surrounding code. The GTK
bindings exist only in the system Python; `dasungctl` re-executes itself
there when started from this environment. For a GUI smoke test without
hardware, run
`/usr/bin/python3` with `PYTHONPATH=src:tests`, build `TrayApp` (and the
windows from `tray_windows`) against `tests.fakes.FakeTransport` and enter
`Gtk.main()` only when needed for screenshots.

## Protocol evidence

`docs/protocol.md` holds the confirmed wire format, the exact captures and
the open questions; keep it in sync when changing `protocol.py` or
`client.py`. The distilled static analysis of the official clients is in
`docs/research-findings.md`, and the physical ghosting measurements are in
`docs/ghosting-experiments.md`. Review the exact frame and its evidence
before sending state-changing frames to the hardware; only one process can
hold the monitor at a time (lock in `$XDG_STATE_HOME/dasungctl/`).

## Panel profiles

Everything model-specific — mode numbers, speed labels, frontlight presets,
temperature mapping, read fields and limits — lives in a `PanelProfile` in
`src/dasungctl/panels.py`; the `panel` config key selects it and the code
reads it through `get_panel()`. Adding a model is described in
[docs/panels.md](panels.md); never copy calibrations between models, and
keep tests in `tests/test_panels.py` in step with the tables.

## Ghost photos

`tools/ghost_photo.py` crops the same panel area in every photo and reports
simple gray-level statistics (a uniform canvas with visible ghosting has a
higher standard deviation). The reference photo lets the tool subtract
illumination/JPEG noise; the crop must be identical across photos.

```console
.venv/bin/python tools/ghost_photo.py --crop 300x300+400+300 \
  --reference clean.jpg --noise-reference clean-repeat.jpg \
  --json before.jpg after.jpg
```

See [ghosting-experiments.md](ghosting-experiments.md) for the measurement
method that uses it.

## Wayland backend status

The ScreenCast/PipeWire capture path for the ghost estimate
(`screencap.WaylandCapture`) and the KWin window labels
(`windows.KWinZones`) exist in the code but are **experimental and outside
the tested configuration** for 0.1: the tested setup is Linux Mint on X11,
the zone-clearing overlay is not available on Wayland at all, and the
compositor permission dialogs make the first sample interactive.

## Releasing

See [releasing.md](releasing.md).
