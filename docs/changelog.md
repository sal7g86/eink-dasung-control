# Changelog

All notable changes to this project are documented in this file. The format
follows Keep a Changelog, and versions use semantic versioning.

## 0.1.3 — 2026-10-05

Monitor availability, short status messages, the e-ink terminal theme and
both ghost polarities. The README screenshots still show the dark-only
estimate with its grey preview.

### Added

- **Monitor availability**: the tray combines the serial exchanges with the
  e-ink display output (X11) to decide whether the monitor is usable; when it
  is missing or switched off, `Auto-refresh` and the ghost estimate are
  switched off and the choice is saved, neither switch can be turned back on
  while it is away, and `monitor connected` is announced when it returns (the
  ghost estimate restarts from a clean model, because the panel refreshed
  while it was off).
- **Short status messages**: failures are one short sentence (`monitor not
  found (off or unplugged?)`, `monitor not responding`, `monitor busy —
  another dasungctl is running`, `no access to /dev/ttyUSB0 (permissions)`);
  the full error stays in the log and in the `Controls…` status tooltip.
- **Light ghosts**: the estimator reports both polarities — dark residue on
  light content and light residue on dark content — with the polarity in
  each area's list entry, a warm tint for light areas in the preview, and
  the automatic clearing covering both. Light areas also include dark
  content that was just drawn and is not fully saturated, so the panel
  remains the judge.

### Changed

- The status icon and colour follow a structured severity, no longer the
  message text.
- Docs: the ghost-estimate limitations no longer list the clearing overlay
  (click-through since 0.1.2), note that the alert threshold is configurable
  and that the Wayland label provider is a single instance per process.
- Docs: the ghost-estimate page documents the model's computation (*The
  model*) and an indicative cost profile (*Costs*: sample times, memory,
  capture backends).
- Docs: the README recommends the MIT-licensed
  [konsole-eink](https://github.com/asapelkin/konsole-eink) color scheme for
  terminal work on the panel.

### Fixed

- A lenient read that answered no selector at all is a failure, not a
  successful `reloaded`; `Auto-refresh` no longer fires while the monitor is
  missing.
- A switched-off panel is detected even though the CH340 stays powered and
  its selectors keep answering; while the panel is off the tray never opens
  the serial port (a command sent in that state can wedge the firmware:
  replug the USB cable if it stays mute) and applies the saved configuration
  when it comes back.
- The saved preferences and monitor fields are no longer overwritten with the
  defaults when the tray starts while the monitor is missing.

## 0.1.2 — 2026-10-03

Repository layout cleanup and window fixes.

### Changed

- **Repository cleanup**: `.gitignore`, `MANIFEST.in`, the developer `tools/`
  package and the archived ghosting lab notes are no longer part of the
  public tree (they remain only on the maintainer's disk); the documentation
  that pointed to those notes was reworded.
- **Source layout**: sources moved directly into `src/` (no package
  subdirectory): `pyproject.toml` maps the `dasungctl` import name onto the
  directory and `src/_source_run.py` runs the program from a checkout
  without installing.

### Fixed

- **Window workspace**: `Controls…` and `Ghost estimate…` remain on the
  workspace where they were opened; choosing one again from the menu moves it
  to the workspace in use.
- **Window titles**: `Controls…` and `Ghost estimate…` show their real title
  in the window list and task manager: installing the custom GTK titlebar was
  clearing it.
- **Clearing overlay**: the overlay is really click-through: the input-shape
  call failed silently and the overlay kept the mouse for the duration of a
  flash.

## 0.1.1 — 2026-10-03

The README animation and screenshots were recorded on version 0.1 and do not
show the changes below yet.

### Added

- **Estimate pause**: the `Ghost estimate…` window's `Stop estimate` /
  `Start estimate` button pauses the sampling — and with it the automatic
  clearing, which runs after a fresh sample — while keeping the last result
  on screen. The choice is remembered in the state file across tray restarts,
  like the other switches; `ghost.enabled` in the config stays the master
  switch.

### Changed

- Docs: the release checklist moved into `docs/development.md` (the separate
  `docs/releasing.md` is gone), and the ghosting lab notes moved to
  `docs/archive/ghosting-experiments.md`, off the README's documentation
  table.

## 0.1.0 — 2026-10-02

First public release.

### Added

- **Tray control**: system-tray control (StatusNotifierItem) of a Dasung
  Paperlike HD Revolutionary 13.3 over its CH340 serial interface: display
  mode, contrast, speed, frontlight level, frontlight mode (including the
  project's custom temperature) and soft/hard refresh.
- **Controls window**: `Controls…` with sliders and live values; auto-refresh
  timer with 5/10/30 second and 1/2/3/5/10 minute intervals; last
  configuration automatically saved and restored at startup.
- **Configuration**: optional JSON configuration in
  `$XDG_CONFIG_HOME/dasungctl/config.json`.
- **Logging**: per-run log file in `$XDG_STATE_HOME/dasungctl/dasungctl.log`,
  rewritten at every command start and mirrored on the terminal when the tray
  runs from a terminal; clean exit on Ctrl+C and SIGTERM.
- **Diagnostics**: serial diagnostics `dasungctl devices` and
  `dasungctl doctor` (also `--json`).
- **Ghost estimate**: experimental ghost estimate with the `Ghost estimate…`
  window: low-resolution dirty-ink model, window labels (X11 and KDE
  Wayland), threshold alert written to the log, and estimate-driven zone
  clearing through an X11 overlay (no serial frame is sent).
- **Panel profiles**: all model-specific tables live in `panels.py` and are
  selected with the `panel` config key; `doctor` prints the active profile
  and warns on a protocol mismatch. 0.1 ships the confirmed
  `paperlike-hd-13.3` profile only.
- **Documentation**: protocol documentation (`docs/protocol.md`), distilled
  official-client static analysis (`docs/research-findings.md`) and the
  physical ghosting measurements (`docs/archive/ghosting-experiments.md`).

### Changed

- **Ghost alerts**: ghost threshold crossings write a log line instead of a
  desktop notification.
- **Clearing defaults**: the shipped zone-clearing defaults are now the
  maintainer's tuned values: a single grey 168/30 ms pulse, white-black at
  15 ms per phase, a 15 s delay and automatic clearing on.
- **Logging failures**: the tray starts even when the log file cannot be
  written; a logging failure never keeps a command from starting.

### Removed

- **Early CLI**: early command-line subcommands (single reads/writes, raw
  sender, profile/snapshot commands, `watch`, the auto-refresh daemon, the
  curses interface) and the first grid/adaptive zone-clearing implementation,
  with their calibration tools.
- **Legacy migrations**: files from the earlier parameter-heavy config/state
  schemas are rejected with an explicit error naming the unknown field.
- **Research artifacts**: research artifacts, extracted official clients and
  the physical test benches; the durable knowledge moved to `docs/`.
