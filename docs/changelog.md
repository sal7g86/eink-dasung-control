# Changelog

## 0.1.0 — 2026-10-02

First public release.

### Added

- System-tray control (StatusNotifierItem) of a Dasung Paperlike HD
  Revolutionary 13.3 over its CH340 serial interface: display mode, contrast,
  speed, frontlight level, frontlight mode (including the project's custom
  temperature) and soft/hard refresh.
- `Controls…` window with sliders and live values; auto-refresh timer with
  5/10/30 second and 1/2/3/5/10 minute intervals; last configuration
  automatically saved and restored at startup.
- Optional JSON configuration in `$XDG_CONFIG_HOME/dasungctl/config.json`.
- Per-run log file in `$XDG_STATE_HOME/dasungctl/dasungctl.log`, rewritten at
  every command start and mirrored on the terminal when the tray runs from a
  terminal; clean exit on Ctrl+C and SIGTERM.
- Serial diagnostics `dasungctl devices` and `dasungctl doctor` (also
  `--json`).
- Experimental ghost estimate with the `Ghost estimate…` window: low-resolution
  dirty-ink model, window labels (X11 and KDE Wayland), threshold alert
  written to the log, and estimate-driven zone clearing through an X11
  overlay (no serial frame is sent).
- Panel profiles: all model-specific tables live in `panels.py` and are
  selected with the `panel` config key; `doctor` prints the active profile
  and warns on a protocol mismatch. 0.1 ships the confirmed
  `paperlike-hd-13.3` profile only.
- Protocol documentation (`docs/protocol.md`), distilled official-client
  static analysis (`docs/research-findings.md`) and the physical ghosting
  measurements (`docs/ghosting-experiments.md`).

### Changed

- Ghost threshold crossings write a log line instead of a desktop
  notification.
- The shipped zone-clearing defaults are now the maintainer's tuned values:
  a single grey 168/30 ms pulse, white-black at 15 ms per phase, a 15 s delay
  and automatic clearing on.
- The tray starts even when the log file cannot be written; a logging
  failure never keeps a command from starting.

### Removed

- Early command-line subcommands (single reads/writes, raw sender,
  profile/snapshot commands, `watch`, the auto-refresh daemon, the curses
  interface) and the first grid/adaptive zone-clearing implementation, with
  their calibration tools.
- Legacy config/state migrations: files from the earlier parameter-heavy
  schemas are rejected with an explicit error naming the unknown field.
- Research artifacts, extracted official clients and the physical test
  benches; the durable knowledge moved to `docs/`.
