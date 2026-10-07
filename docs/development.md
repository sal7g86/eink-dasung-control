# Development

## Repository layout

- `src/` — the package: `cli.py` (entry point), `tray.py` (GTK
  tray, controller, startup) and `tray_windows.py` (Controls and Ghost
  windows), `client.py`/`protocol.py`/`transport.py` (serial),
  `panels.py` (per-model tables), `controls.py`/`state.py`/`config.py`
  (settings and persistence),
  `ghostwatch.py`/`screencap.py`/`windows.py`/`zoneclear.py` (ghost
  estimate), `doctor.py`/`locking.py`/`paths.py`/`logfile.py`.
  `pyproject.toml` maps the `dasungctl` import name onto this directory;
  `_source_run.py` runs the program from a checkout without installing it.
- `tests/` — offline tests only: fakes, recorded frames, no serial device and
  no hardware. `tests/conftest.py` points every XDG directory at a temporary
  tree.
- `docs/` — the project documentation; the repository root keeps only
  `README.md`.

## Environment and commands

A checkout shared between the two machines holds one environment per machine:
`.venv` on the Linux Mint install (X11) and `.venv-fedora` on the Fedora one
(Wayland, built on the system Python). Use the environment of the machine you
are on, and after moving source files refresh its editable install with
`uv pip install --python <venv>/bin/python -e ".[dev]"`.

```console
# Linux Mint machine (X11)
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

```console
# Fedora machine (Wayland)
uv venv --python /usr/bin/python3 .venv-fedora
uv pip install --python .venv-fedora/bin/python -e ".[dev]"
.venv-fedora/bin/python -m pytest -q
```

Single tests and lint (replace `<venv>` with the environment above):

```console
<venv>/bin/python -m pytest tests/test_tray.py -q
<venv>/bin/python -m pytest -k name
uvx ruff check src tests --select F,E9
```

There is no formatter or type checker configured; only ruff `F,E9` is
expected to pass, so match the style of the surrounding code. The GTK
bindings exist only in the system Python; `dasungctl` re-executes itself
there when started from this environment. For a GUI smoke test without
hardware, run `/usr/bin/python3` with `PYTHONPATH=src:tests`, `import
_source_run` (it maps the `dasungctl` name onto `src/`), then build `TrayApp`
(and the windows from `tray_windows`) against `tests.fakes.FakeTransport` and
enter `Gtk.main()` only when needed for screenshots.

## Protocol evidence

`docs/protocol.md` holds the confirmed wire format, the exact captures and
the open questions; keep it in sync when changing `protocol.py` or
`client.py`. The distilled static analysis of the official clients is in
`docs/research-findings.md`, and the physical ghosting measurements were
taken during development. Review the exact frame and its
evidence before sending state-changing frames to the hardware; only one
process can hold the monitor at a time (lock in `$XDG_STATE_HOME/dasungctl/`).

## Panel profiles

Everything model-specific — mode numbers, speed labels, frontlight presets,
temperature mapping, read fields and limits — lives in a `PanelProfile` in
`src/panels.py`; the `panel` config key selects it and the code
reads it through `get_panel()`. Adding a model is described in
[docs/panels.md](panels.md); never copy calibrations between models, and
keep tests in `tests/test_panels.py` in step with the tables.

## Wayland backend status

The tested configurations are Linux Mint (Cinnamon) on X11 and Fedora (KDE
Plasma) on Wayland. On Wayland:

- the ghost capture goes through the ScreenCast portal
  (`screencap.WaylandCapture`); the first start shows the share dialog unless
  a granted restore token is already stored, and later starts reuse it
  silently (verified on KDE);
- the window labels come from KWin's scripting interface
  (`windows.KWinZones`), one provider instance per process;
- the panel-presence check (`screencap.drm_output_present`) matches the
  panel profile's EDID names against the connected DRM outputs in
  `/sys/class/drm`, so a switched-off panel is detected as on X11;
- zone clearing uses layer-shell overlays (`zoneclear.WaylandZoneFlasher`),
  available on KDE and wlroots compositors; GNOME does not expose
  layer-shell to applications, so there the feature reports itself
  unavailable;
- moving the tray windows to the desktop in use runs the KWin script
  `windows.KWIN_MOVE_SCRIPT`, matching the `dasungctl` resource class.

GNOME Wayland and wlroots compositors are untested: capture should work
everywhere through the portal, labels need KDE's KWin scripting, and clearing
needs layer-shell. The X11 path stays the reference.

## Releasing

The release history lives in [changelog.md](changelog.md); this is the
checklist for cutting a release. Every user-visible feature or change is
recorded in the changelog, under the section of the version being prepared,
as soon as it is made; when the README animation or the screenshots no
longer match the interface, say so there too.

1. Update `version` in `pyproject.toml` (semantic versioning).
2. Check the changelog section for that version and give it the release date.
3. Run the full verification:

   ```console
   .venv/bin/python -m pytest -q
   uvx ruff check src tests --select F,E9
   ```

4. Review the tree for files that must not be published (`git status`,
   `.gitignore`); official-client artifacts, research binaries and the
   maintainer-local tools stay outside version control.
5. Commit the release state.
6. Tag and push:

   ```console
   git tag -a v0.1.1 -m "dasungctl 0.1.1"
   git push origin main --follow-tags
   ```

   The repository needs a remote first:

   ```console
   git remote add origin git@github.com:sal7g86/eink-dasung-control.git
   ```

7. Create the GitHub Release for the tag, using the changelog section as the
   release notes. GitHub attaches the source archives automatically.
