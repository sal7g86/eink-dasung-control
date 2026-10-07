# The tray

`dasungctl tray` puts the monitor controls in the system tray through the
StatusNotifierItem protocol, so one application works on KDE (native), on
GNOME (through the AppIndicator extension) and on Cinnamon (through XApp).
The `tray` word is optional, and the serial options override the
configuration:

```console
dasungctl
dasungctl tray
dasungctl --device /dev/ttyUSB1 --timeout 2.0 --interval 300
dasungctl --device /dev/ttyUSB1 tray
dasungctl tray --install-autostart   # ~/.config/autostart/dasungctl-tray.desktop
dasungctl tray --install-launcher    # ~/.local/bin/dasungctl
```

`--device` and `--timeout` override the serial settings of the configuration
and `--interval` the auto-refresh timer (seconds); `--config` selects another
configuration file. `--install-autostart` writes the login entry and
`--install-launcher` the `dasungctl` command on `PATH` (a script that runs
this checkout with the system Python and reports a missing disk); both exit
without starting the tray.

The tray runs with the system Python, where the GTK bindings live; started
from an interpreter without them — the project virtualenv, a `pipx`
installation — the command re-executes itself automatically. Only one
process at a time can hold the monitor, so concurrent actions report that
the monitor is busy.

## Menu and windows

The menu offers `Controls…`, mode, contrast, speed (labelled with the
official `Fast`..`Fast++++` names), the ten frontlight levels (`Off`,
`1`..`10`), frontlight mode with its presets and a `Custom` submenu holding
ten temperature levels, soft/hard refresh, reload, and an auto-refresh timer
with 5/10/30 second and 1/2/3/5/10 minute intervals.

Every entry carries a symbolic theme icon; the groups whose choices also carry
icons (Mode, Frontlight mode) and Auto-refresh show the current value in the
parent label — `Mode: video`, `Auto-refresh: off` — while Contrast, Speed,
Frontlight and Interval keep the native radio marks. The `Controls…` window
groups the settings into `Image`, `Light`, `Automation` and `Actions` cards
with sliders, live values, and a status line for the last result; the
temperature slider shows up in the `Light` card only while `Frontlight mode`
is `Custom` or `mixed` (the firmware reads the project's custom value as
mixed). The panel tooltip shows the full state and, on panels that render
indicator labels (such as KDE), the label shows the current mode.
`Ghost estimate…` opens the experimental ghost estimate
(see [ghost-estimate.md](ghost-estimate.md)): it captures only the panel's
monitor — the Dasung region of the root window on X11, the monitor picked in
the share dialog on Wayland.
Both windows remain on the workspace where they are opened; choosing one
again from the menu moves it to the workspace in use (on KDE Wayland the
tray asks KWin; on other compositors that choice belongs to the compositor).

The first menu row is the status line (`reloaded`, `monitor changed: …`,
`auto-refresh on`, …). Failures are compressed to one short sentence —
`monitor not found (off or unplugged?)`, `monitor not responding`,
`monitor busy — another dasungctl is running`, `no access to /dev/ttyUSB0
(permissions)` — while the full error stays in the log and in the tooltip of
the `Controls…` window's status line; the row's icon and colour follow the
severity, not the text.

## Monitor availability

Two signals decide whether the monitor is usable. The serial exchanges are
the first: after two failed reads in a row the tray declares the monitor
unavailable. The second is the e-ink display output: the panel's HDMI
receiver disappears when it is switched off, while the CH340 stays powered
and keeps answering the stored values, so the output is the only confirmed
way to tell "off" from "on". On X11 the check reads the Gdk monitors' EDID
names through python-xlib; on Wayland it reads the connected DRM outputs in
`/sys/class/drm` and matches the same names. The startup check is conclusive
immediately; a later miss needs two in a row, so a display reconfiguration
does not switch the automatic features off. Without python-xlib on X11,
without `ghost.output` naming the output, or when no output data is
readable, the check cannot tell and only the serial exchanges are used.

A panel known to be off is never talked to: the tray skips `Reload from
monitor`, the refresh actions and the serial poll, and a tray started with
the panel off skips the saved configuration (it is applied when the panel
returns). A command sent to a switched-off panel can wedge the monitor's
serial firmware: if the tray keeps reporting `monitor not responding` while
the panel is on, replug the monitor's USB cable.

In every case the tray switches `Auto-refresh` and the ghost estimate off and
saves the choice. Neither switch can be turned back on while the monitor is
unavailable; when it returns the tray writes `monitor connected`, and the
ghost estimate starts from a clean model only if the user enables it again
(the panel refreshed while it was off).

Started from a terminal, the tray mirrors its log lines on stderr and Ctrl+C
exits cleanly like the `Quit` menu entry; SIGTERM (for example at session
logout) is handled the same way. The exit saves the ghost estimate, closes
the screen-capture session and destroys the clearing overlays; see
[configuration.md](configuration.md) for the log file.

## Refresh

The `Refresh` submenu and the two buttons in the `Controls…` window send the
official clients' refresh frames `5FF50300000000000000A0FA` (soft) and
`5FF50301000000000000A0FA` (hard). They are global refresh actions in the
analyzed clients; neither contains regional coordinates. The monitor's
physical `C` button is invisible to the tray, so after using it press
`Reset estimate`.

## Frontlight modes and custom temperature

`Frontlight mode` cycles the calibrated presets (off, cold, warm, mixed) plus
the project's `custom` value, shown by name in the tray menu and in the
`Controls…` window. Selecting `cold`, `warm`, or `mixed` sends a temperature
before the mode (`100`, `0`, and `70`), because the serial mode write alone
was not observed to apply it; `off` leaves the stored balance untouched.

`custom` offers ten temperature levels in the tray menu, from `1 (cold)`
(`100`) to `10 (warm)` (`0`), evenly interpolated; the `Controls…` window
shows the same levels on its slider. The firmware accepts `0..100` for the
temperature and clamps higher bytes to `100`, and it has no `4` value for the
mode: writing `4` reads back as `3` (mixed) with the temperature applied, so
the manual control shows up while the mode is `mixed` too. The tray maps that
read-back back to `custom` while the read temperature equals the last manually
set one, so the menu keeps showing `custom`; a mixed preset or a physical lamp
press keeps `70` and still reads as `mixed`. Picking a level or moving the
slider sends the temperature and then switches `Frontlight mode` to the
project's custom value `4`; no read-back follows, because the monitor ignores
reads that arrive right after a write, so the write acknowledgement is the
only confirmation. Choosing `Custom` in the `Controls…` dropdown without
touching the slider reuses the last temperature set manually, so switching to
another preset and back does not lose it: the value is remembered for the
session and seeded from the monitor when the tray reads a custom frontlight
mode. Nothing reads `temperature` back right after a mode write: the monitor
stalls those reads, so use `Reload from monitor` or wait for the tray's poll.
Value `4` is a project hypothesis not present in the official clients and not
implemented by the firmware. `mux` is not exposed by the tray.

## Physical controls

The six buttons were calibrated against the serial read-backs:

| Control | Action |
| --- | --- |
| `C` | one-key clear ghost (visible full-screen refresh, no readable state) |
| `M` | cycles display mode (auto/text/graphic/video) |
| `-` / `+` | contrast down/up (nine steps) |
| hold `M`, press `-` / `+` | speed (ink density) down/up, five levels |
| lamp | cycles frontlight mode: mixed, cold, warm, off |
| hold lamp, press `-` / `+` | brightness down/up: `Off` plus ten levels, wraps around |
| power | on/off; settings persist across power cycles |

The lamp cycle maps to `frontlight_mode` values `3` (mixed), `1` (cold), `2`
(warm), and `0` (off), and a lamp press also re-applies a level and a
temperature. Holding the lamp button while pressing `-`/`+` cycles the
brightness: `Off` plus ten levels (`10`..`100`), wrapping around at both ends.
Holding `M` while pressing `-`/`+` changes `speed` (the tray's
`Fast`..`Fast++++` levels), which is otherwise only settable from the tray.
While the tray runs, it mirrors all of these physical changes into the menu
and the saved configuration within a few seconds.

## Parameter notes

The firmware accepts `0..100` for the temperature and clamps higher bytes to
`100` (measured 2026-09-28): `100` cold and `0` warm, with the tray's ten
levels interpolating evenly between them. The frontlight brightness is
calibrated on this unit: `0` off plus ten levels of `10` (`10`..`100`), shown
in the tray as `Off`, `1`..`10`; values above `100` are clamped to level `10`.
Frontlight-mode values are calibrated on this unit: `0` off, `1` cold, `2`
warm, `3` mixed; the project's `4` reads back as `mixed` (see
[protocol.md](protocol.md)). Speed is a five-level value changed physically
with `M` + `-`/`+` and independent of the display mode; the tray labels values
`1..5` with the official `Fast`..`Fast++++` names in combo order, a mapping
that is not monitor-confirmed. Changing the display mode re-applies a stored
per-mode contrast on this unit (see [protocol.md](protocol.md)), so the
contrast shown in the tray can differ from the last value set until `Reload
from monitor` re-reads it. `mux` semantics remain unconfirmed: the target
reported `1` and later `3` without an identified cause. On this test unit the
firmware acknowledges writes for commands `01`, `03`, `04`, `05`, `07`, and
`0B` with `5FF5F0CC000000000000A0FA`, but does not implement text enhancement
(`0x12`) or dithering (`0x20`), matching Dasung's introduction of text
enhancement with the newer 13K-series models. Selectors `03` and `11` have no
established meaning. Command `0x0C` (firmware-update state) is intentionally
not exposed.

## Serial management and diagnostics

Writes are verified: each one waits for the monitor's `F0 CC`
acknowledgement and fails if it does not arrive. The monitor occasionally
emits stale frames, omits an acknowledgement, or emits unsolicited state
frames after a write (for example a frontlight-level report), so the client
skips up to eight frames looking for the acknowledgement, re-sends the
(idempotent) write only when the exchange times out, and skips unsolicited
frames that do not match the expected response before reporting an error.
Only one `dasungctl` session can hold the serial port at a time.

To keep every action quick, the tray reads back only the six selectors it
displays and persists, and caches the auto-detected CH340 path between
operations, re-enumerating it only when an open fails.

When the tray cannot reach the monitor, diagnose the connection and the
firmware support of every selector:

```console
dasungctl devices             # list serial devices without opening them
dasungctl doctor
dasungctl doctor --json
```

## Scope and limitations

`dasungctl` 0.1 targets one panel and one tested platform:

- **Panel**: only the Dasung Paperlike HD Revolutionary 13.3 (40 Hz,
  protocol `0x30`), the HD-FT variant with frontlight and touchscreen. Other
  models and protocol families are not supported and
  have not been tested; their parameters and commands can differ. All
  panel-specific values live in one profile, so adding a model means filling
  that profile after capturing the same evidence
  ([panels.md](panels.md)).
- **Platform**: Linux with a GTK 3 desktop. Tested on Linux Mint (Cinnamon,
  X11) and Fedora (KDE Plasma, Wayland). On Wayland the capture goes through
  the ScreenCast portal, the window labels through KWin scripting, the
  panel detection through DRM sysfs, and zone clearing through layer-shell
  on KDE and wlroots compositors (not on GNOME, which does not expose
  layer-shell to applications). Other Wayland compositors are untested; see
  [development.md](development.md).
- Writes are verified against the monitor's acknowledgement and only one
  `dasungctl` session can hold the serial port at a time. No loop, range
  scanner or fuzzer is included.
- There is no serial regional refresh: zone clearing is a software overlay
  (an X11 window or a Wayland layer-shell surface). The CH341 programmer
  interface, HDMI DDC/I2C, the touchscreen and the vendor-specific HID
  interface are outside the current implementation.
