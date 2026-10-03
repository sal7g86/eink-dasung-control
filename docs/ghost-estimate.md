# Ghost estimate

`Ghost estimate…` opens a diagnostic window with an estimate of the ghosts the
e-ink panel is probably still showing: dark shapes on a neutral background,
the estimated areas outlined, and a list with each area's application, its
position in screen pixels, size, severity and age. When the level crosses the
configured threshold the tray writes one line to its log — no desktop
notification — and re-arms the alert after a soft/hard refresh or the `Reset
estimate` button. The preview is deliberately ghost-only: the point is to
compare where the estimator thinks the ghosts are with the physical panel.
On X11 the window stays on every virtual desktop, so it follows the
workspace in use; on Wayland that choice belongs to the compositor.

The estimate is an **experimental** feature in 0.1: the model constants are
assumptions to calibrate against the panel, and only a photo of the panel can
confirm a real ghost.

## How it works

It watches the captured screen, updates a low-resolution model of the panel
only where the content changed, and remembers old dark ink that was not fully
erased; a dark pixel that is merely lighter than intended is not counted. It
sees only what happened while the tray was running, and the monitor's
physical `C` button is invisible to it: press `Reset estimate` after using
it. The `Retry capture` button recovers after a refused screen share.

The header's `Stop estimate` button pauses the sampling — and with it the
automatic clearing, which runs after a fresh sample — while the last result
stays on screen; `Start estimate` resumes both, and the choice is remembered
across tray restarts like the other switches. `ghost.enabled` in the
configuration remains the master switch that can disable the feature
entirely.

Each area carries the application class that was over it when the ghost
formed (`konsole (fullscreen)`, `firefox`), so the list answers "which window
left it". On X11 the window list comes from EWMH through `python-xlib` (the
optional `labels` extra; without it the estimate still works, just without
names). On KDE Wayland no standard API exposes other applications' windows,
so the tray asks KWin's scripting interface — the same route tools like
`kdotool` use — and only reads the class, geometry and fullscreen flag, never
window titles. When the label channel is unavailable, the areas keep their
plain coordinates.

While nothing changes the sampling interval doubles from `interval` up to
`max_interval`, so an idle screen costs almost nothing; the first change, and
an open `Ghost estimate…` window, return to the base cadence. The threshold line
in the log can therefore be delayed up to `max_interval` after a change.

## Zone clearing

Too much ghost on one area can be flashed locally: the tray covers the area
with a borderless overlay that flashes the area (white-black phases or a
single grey pulse), so the controller rewrites those pixels. It is a software
zone clear, not a serial command: the confirmed protocol has no regional
refresh, and no invented coordinate frame is ever sent. The refresh stays
global and manual.

The `Ghost estimate…` window has an `Automatic clearing` switch (on by default,
remembered across restarts) that flashes every due area in one wave, a
`Clear areas` button that flashes every estimated area at once, and a
`Clearing settings` section for the flash colors, the phase durations and the
delay; `Test flash` previews the current settings on a large square (half of
the panel's smaller side) at the center of the monitor. Automatic clearing is
just "recognize, wait, flash together": every area the estimator reports (a
rectangle in the preview) whose age reached `delay` seconds is flashed in the
same wave, each with its own overlay and the same phases, even while the
screen is in use; the areas seen in the preview are flashed whole, however
large. The shipped defaults are the maintainer's tuned values: a single grey
pulse of 168/30 ms, white-black at 15 ms per phase and a 15 s delay; 20 ms
per phase is reported to clean well too, and longer phases are the safe
starting point if the short ones stop working. The physical efficacy is not
proven: the area is only rewritten, so a heavy ghost can leave a residue that
needs a global refresh. Judge it on the panel, and treat the fewer, larger
flashes as the better trade.

Local clearing is available on **X11 only**: on Wayland a window cannot be
placed at global coordinates, and the tray reports the feature as
unavailable.

## Capture backends

On Wayland the first sample asks for a screen share and the Dasung monitor
must be picked in the dialog. The tray registers a stable application id
(`dasungctl`) with the portal, so the grant and its restore token can survive
the next start; if the compositor asks again, pick the monitor once more.
KWin's own screenshot API is allowlisted to installed screenshot
applications, so the tray uses the ScreenCast portal and reads the PipeWire
stream with GStreamer; no image is written to disk.

On X11 the Dasung region of the root window is captured directly; the
automatic detection reads the monitors' EDID model names through
`python-xlib`, since Gdk only reports the RandR output name there, and
matches them against the active panel profile's `edid_names`; `ghost.output`
selects the monitor when that fails, by model or by output name (`DP-1`).

## Known limitations

- The model counts only dark ghosts (old dark content on current light
  content); `E = A - S` positive errors are imperfect ink, not counted.
  `noise`, `min_error`, `gamma_ink`, `gamma_erase` and the alert `threshold`
  are assumptions, not yet configurable: compare the estimate with the panel
  over a few sessions and tune them.
- On KDE Wayland a combined run once produced an element without its
  application label. The suspected cause is two `KWinZones` instances
  registering the same D-Bus object in one process; it is still to be
  re-tested, and the X11 provider is unaffected.
- The restore-token reuse on Wayland was verified once (the dialog did not
  reappear), but a second silent start is still to be confirmed.
- The clearing overlay is not fully click-through: `ZoneFlasher._pass_through`
  calls PyGObject's `input_shape_combine_region` with the wrong signature and
  the exception is swallowed, so the overlay blocks the mouse for the
  duration of a flash (about 0.6 s). Fix and verify with `Test flash`.

## Implementation notes

These are the non-obvious points future changes must keep in mind.

- Portal calls must subscribe to `Response` *before* the method call: a valid
  restore token can answer immediately, and an earlier version lost that
  race. `screencap._call` predicts the request path from the sender and the
  handle token.
- On X11, python-xlib's `translate_coords(self, src, x, y)` translates from
  `src` to `self`: ask the *root* for a window position
  (`root.translate_coords(window, 0, 0)`), because the reverse call returns
  the negated global position and silently clips everything away.
- Gdk on X11 reports the RandR output name as `get_model()`; the real model
  comes from the EDID `0xFC` descriptor, readable through `Xlib.ext.randr`
  output properties and matched to the Gdk monitor by RandR CRTC geometry
  (`screencap.x11_monitor_aliases`).
- The clearing overlay must be an override-redirect window (`realize()` then
  `Gdk.Window.set_override_redirect(True)`): a managed window spends most of
  the first phase in the desktop's map animation, and the phase timer starts
  on the map event for the same reason. The overlay's WM class
  (`dasungctl-clear`) is in `windows.IGNORED_APPS`, and the tray pauses
  sampling while a flash is on screen, so the estimate never mistakes the
  overlay for content.
- Automatic clearing is one rule: `zoneclear.due_clear_elements` returns
  every area whose `age` reached `clear.delay`, and the tray flashes them in
  one wave — one override-redirect window per rectangle, all sharing one
  phase timer, so they light up and go dark together — even while the screen
  is in use. Each flashed rectangle is then reset in the estimate. The manual
  `Clear areas` button runs the same wave with every detected area. There is
  no splitting: an area is flashed whole, however large.
- Response code 1 from the portal means the user dismissed the dialog:
  `PortalRefused` is raised and the backend does not retry with a second
  dialog.
- `Registry.Register("dasungctl", {})` before the first session gives a
  stable portal application id.
- PyGObject has no `register_object_with_closures`; `windows.KWinZones` uses
  the deprecated `register_object` with a local warning filter.
- The KWin script lives in `$XDG_RUNTIME_DIR/dasungctl-zones.js`, plugin name
  `dasungzones`; the report comes back through `callDBus` to
  `org.dasungctl.Zones`.
- The model counts only dark ghosts; a cell is dirty above
  `max(24 model px, 5% of its area)` with `|E| >= 8`; the label is recorded
  when the cell first becomes dirty and kept while it stays dirty.
- Sampling backs off by doubling up to `max_interval` while samples show no
  changes; `set_force_base(True)` (the open window) restores the base rate.
- With a static screen, frames differ only by capture jitter below the
  per-pixel noise floor, so `changed_pixels` can legitimately stay 0 for many
  samples.

The physical measurements that led here — refresh efficacy, the removed grid
and adaptive clearing modes, the rejected low-amplitude maintenance — were
taken during development.
