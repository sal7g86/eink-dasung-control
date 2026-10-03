# Configuration, state and log

## `config.json`

Configuration lives in `$XDG_CONFIG_HOME/dasungctl/config.json`
(`~/.config/dasungctl/config.json` by default) and is optional:

```json
{
  "device": "auto",
  "timeout": 1.0,
  "panel": "paperlike-hd-13.3",
  "autorefresh": { "interval": 300, "hard": false },
  "ghost": {
    "enabled": true,
    "interval": 2.0,
    "max_interval": 30,
    "threshold": 20,
    "output": "auto",
    "width": 480,
    "clear": {
      "enabled": true,
      "style": "single",
      "white_level": 255,
      "white_ms": 15,
      "black_level": 0,
      "black_ms": 15,
      "grey_level": 168,
      "grey_ms": 30,
      "delay": 15
    }
  }
}
```

- `device` is `auto` or a serial path; `timeout` is in seconds.
- `panel` selects the monitor profile: the model-specific mode table, speed
  labels, frontlight presets, temperature mapping and field limits. The only
  confirmed profile is `paperlike-hd-13.3`; an unknown or misspelled key is
  rejected with the list of known names. See [panels.md](panels.md) for how
  another model is added after capturing its evidence.
- `autorefresh` sets the starting interval and whether the timer uses the hard
  refresh frame (`false` = soft).
- `ghost` configures the ghost estimate (see
  [ghost-estimate.md](ghost-estimate.md)): `interval` is the base sampling
  period in seconds, `max_interval` the cap reached while nothing changes,
  `threshold` the 0-100 level that writes the threshold alert to the log,
  `output` the monitor name substring for X11 (ignored on Wayland), and
  `width` the model's horizontal resolution (higher costs more CPU per
  sample).
- `clear` is deliberately simple. Every area the estimator reports (a
  rectangle in the `Ghost estimate…` preview) is flashed once it has been dirty for
  `delay` seconds; all the areas due at that moment are flashed together in
  one wave. `style` is `white-black` (a bright phase then a dark one) or
  `single` (a soft grey pulse), and each style has its phase level
  (`white_level`, `black_level`, `grey_level`, 0-255) and duration
  (`white_ms`, `black_ms`, `grey_ms`, 1-2000 milliseconds). The shipped
  defaults are the maintainer's tuned values — a single grey 168/30 ms pulse,
  white-black at 15 ms per phase, a 15 s delay and automatic clearing on —
  and the `Defaults` button in the clearing editor restores them.

Unknown keys and invalid values are rejected with an error naming the file
and the field, so a typo never silently changes behaviour. An old key from a
removed feature (`ghost.notify`, `ghost.clear.min_age`, ...) is an unknown
key like any other.

Named profiles were removed on 2026-09-26: the tray remembers the last
configuration automatically (see below). The config values are only the
first-run defaults for the tray's own preferences; once the tray has saved a
switch or a clearing setting, the state file wins.

## `last-state.json`

Every successful change (menu or `Controls…` window) is written to
`$XDG_STATE_HOME/dasungctl/last-state.json` and applied again when the tray
starts, so the settings survive tray restarts and monitor power cycles. The
file stores the monitor values the tray can change (mode, contrast, speed,
frontlight, temperature, and frontlight mode) plus the tray's own
preferences:

- `autorefresh` and `autorefresh_interval`: a timer left on comes back on with
  the same interval;
- `ghost_clear`: the automatic-clearing switch and the whole clearing-settings
  object edited in `Ghost estimate…`;
- `ghost_estimate`: whether the ghost estimate was running; the Stop/Start
  button in `Ghost estimate…` writes it, so a stopped estimate stays stopped
  across restarts (`ghost.enabled` in the config is the master switch).

While the tray runs it also reads the display fields every few seconds
(`POLL_SECONDS`), so changes made with the monitor's physical buttons update
the menu and the saved file too. A missing file means the tray just reads the
monitor and uses the config defaults; a corrupt or stale one is reported in
the tooltip and ignored. To forget a stored setting, edit or delete the file,
or use `Reload from monitor` to mirror the monitor's current values.

## Other state files

- `$XDG_STATE_HOME/dasungctl/ghost.json`: the estimate summary (level, areas,
  labels) and the threshold-alert bookkeeping. Its `notified_at` and `armed`
  keys keep their historical names even though the alert is a log line and
  there is no desktop notification.
- `$XDG_STATE_HOME/dasungctl/screencast.json`: the ScreenCast restore token
  granted by the user on Wayland (see [ghost-estimate.md](ghost-estimate.md)).
- `$XDG_STATE_HOME/dasungctl/lock`: the exclusive-access lock; only one
  process can hold the monitor at a time.

## Log

Every command rewrites `$XDG_STATE_HOME/dasungctl/dasungctl.log` at startup:
the previous run's lines are discarded, so the file always describes the
current run. The tray logs the device and the settings it starts with, every
serial action and error, the ghost threshold alerts and the clearing waves,
and the reason it exits (menu, Ctrl+C or SIGTERM). `devices` and `doctor`
record their results, and configuration or state errors are written next to
the message printed on the terminal.

When the tray runs attached to a terminal every line is mirrored on stderr;
when the disk or the state directory is not writable the command still starts
and the lines go to the terminal only. Each line has a timestamp and a level
(`info`, `warn`, `error`) and is flushed immediately, so `tail -f` follows a
running tray.
