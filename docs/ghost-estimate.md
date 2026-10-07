# Ghost estimate

`Ghost estimate…` opens a diagnostic window with an estimate of the ghosts the
e-ink panel is probably still showing: the estimated areas outlined over a
ghost-only map (dark residue in grey, light residue in a warm tint), and a
list with each area's application, its position in screen pixels, size,
severity, polarity and age. When the level crosses the configured threshold
the tray writes one line to its log — no desktop notification — and re-arms
the alert after a soft/hard refresh or the `Reset estimate` button. The
preview is deliberately ghost-only: the point is to compare where the
estimator thinks the ghosts are with the physical panel.
On X11 the window remains on the workspace where it was opened; choosing it
again from the tray menu moves it to the workspace in use. On KDE Wayland
the tray asks KWin for the same move through its scripting interface; on
other compositors that choice belongs to the compositor.

The estimate is an **experimental** feature in 0.1: the model constants are
assumptions to calibrate against the panel, and only a photo of the panel can
confirm a real ghost.

## How it works

It watches the captured screen, updates a low-resolution model of the panel
only where the content changed, and remembers old ink that was not fully
rewritten: a dark residue where light content replaced dark content, and a
light residue where dark content replaced light content. Light areas also
cover dark ink that has just been drawn and is not fully saturated, so a
freshly opened dark window can show light areas until those pixels are
rewritten. It sees only what happened while the tray was running, and the
monitor's physical `C` button is invisible to it: press `Reset estimate`
after using it. The `Retry capture` button recovers after a refused screen
share.

The header's `Stop estimate` button pauses the sampling — and with it the
automatic clearing, which runs after a fresh sample — while the last result
stays on screen; `Start estimate` resumes both, and the choice is remembered
across tray restarts like the other switches. `ghost.enabled` in the
configuration remains the master switch that can disable the feature
entirely. A missing monitor turns the estimate off too (the tray saves the
choice): this includes a panel that is switched off, detected from the e-ink
display output because the serial interface keeps answering; the window says
`The monitor is unavailable`, `Start estimate` stays disabled until it
answers, and the model and the capture's monitor are reset when it returns,
because the panel refreshed while it was off.

Each area carries the application class that was over it when the ghost
formed (`konsole (fullscreen)`, `firefox`), so the list answers "which window
left it". On X11 the window list comes from EWMH through `python-xlib` (the
optional `labels` extra; without it the estimate still works, just without
names). On KDE Wayland no standard API exposes other applications' windows,
so the tray asks KWin's scripting interface — the same route tools like
`kdotool` use — and only reads the class, geometry and fullscreen flag, never
window titles. When the label channel is unavailable, the areas keep their
plain coordinates and the estimate window says why the query failed.

While nothing changes the sampling interval doubles from `interval` up to
`max_interval`, so an idle screen costs almost nothing; the first change, and
an open `Ghost estimate…` window, return to the base cadence. The threshold line
in the log can therefore be delayed up to `max_interval` after a change.

## The model

For each pixel the estimator keeps three values: `S`, the current screen
content; `A`, what the panel is estimated to show; and the signed ghost error
`E = A - S` (negative: darker than the content, positive: lighter).

One sample walks the captured frame and, only where the content changed by
more than `noise` (10 levels out of 255), moves `A` toward `S` by an
efficiency `gamma` that depends on the direction: `gamma_ink` (90) when the
pixel darkens, `gamma_erase` (80) when it lightens. The update never reaches
`S` exactly, so part of `E` remains and stays there — untouched pixels keep
their error — until the area is rewritten or the estimate is reset. `noise`,
`min_error`, `gamma_ink` and `gamma_erase` are model assumptions, not user
settings.

`E` is classified by polarity and magnitude. A dark ghost is `E <= -min_error`
(8) on content at or above 128: old dark ink still visible on light content.
A light ghost is `E >= min_error` on content below 128: old light content
still visible on dark content, plus dark ink that has just been written and
is not fully saturated. The content gates partition the pixels, so a pixel
counts for at most one polarity.

The model groups pixels into square cells (16 columns; the rows follow the
aspect ratio). A cell is dirty for a polarity when at least
`max(24 model pixels, 5% of its area)` carry that polarity with an error over
`min_error`; dirty cells of the same polarity are joined by 8-connectivity
into the areas shown in the window, so a dark area and a light halo touching
it remain two elements. Each area reports its bounding box in screen pixels,
its severity (100 when its counted pixels are off by 128 levels on average),
its age (since the cell first became dirty) and the application recorded at
that moment, which is why a label survives the window closing.

The window's `level` is the total absolute error over the whole model, 100
when every model pixel is off by 32 levels; `light_level` is the part
contributed by light ghosts. The preview maps the errors to a ghost-only
image: negative on light content as darker grey, positive on dark content as
a warm tint (a light residue lighter than the preview's background could not
be seen on its own), with the faint cell grid and each area outlined in its
polarity's colour. The panel is never read back: only a photo can confirm a
real ghost. `Reset estimate` models a full panel refresh; a zone flash resets
the estimate over the flashed rectangle at cell granularity.

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

Local clearing needs a placement route for its overlay. On X11 it is an
override-redirect window at global screen coordinates; on Wayland it is a
layer-shell surface anchored to the captured output, which KDE and wlroots
compositors provide but GNOME does not — there the tray reports the feature
as unavailable.

## Capture backends

On Wayland the first sample asks for a screen share and the Dasung monitor
must be picked in the dialog. The tray registers a stable application id
(`dasungctl`) with the portal, so the grant and its restore token survive
the next start: with the stored token later starts open the session silently
(verified on KDE), and only a refused or expired grant asks again. KWin's
own screenshot API is allowlisted to installed screenshot applications, so
the tray uses the ScreenCast portal and reads the PipeWire stream with
GStreamer; no image is written to disk.

On X11 the Dasung region of the root window is captured directly; the
automatic detection reads the monitors' EDID model names through
`python-xlib`, since Gdk only reports the RandR output name there, and
matches them against the active panel profile's `edid_names`; `ghost.output`
selects the monitor when that fails, by model or by output name (`DP-1`).

The capture covers only the panel's monitor on both backends: the estimate
never sees the rest of the desktop. The label channel reads window geometry
and class only — never titles or pixels — and the panel-presence check reads
the outputs' EDID and connection state.

## Costs

The estimate runs inside the tray process, on the GTK main loop, in plain
Python: there is no worker thread and no helper process, and it never opens
the serial port. A timer wakes the tray once per second and asks the watcher
whether a sample is due; `ghost.interval` (2 s) is the base cadence, a sample
with no changed pixels doubles the wait up to `ghost.max_interval` (30 s),
and the first change and an open `Ghost estimate…` window return to the base
cadence. An idle screen therefore costs almost nothing between samples, at
the price of a threshold alert late by up to `max_interval`.

Each due sample captures the monitor and updates the model. Indicative
measurements on the maintainer's machine (Python 3.13, a 2200×1650 monitor at
the default `width` 480):

| Step | Cost |
| --- | --- |
| frame capture and reduction | backend-dependent; on Wayland the conversion is done by GStreamer, on X11 by GDK plus a Python grey pass |
| X11 grey conversion, 480×360 RGBA | about 16 ms |
| model update, 480×360 | 13 ms with ~10% of the pixels changed, 32 ms on a whole-frame change, 0.15 ms on an identical frame |
| model update, 1200×900 (`ghost.width` 1200) | 84 ms, up to 198 ms on a whole-frame change |
| preview, 300 px wide | about 9 ms, only while the `Ghost estimate…` window is open |
| model memory | about 8 bytes per model pixel: ~1.4 MB at 480×360, ~8.6 MB at 1200×900 |

These are reference measurements, not guarantees. The capture backend shapes
the cost too: on Wayland the ScreenCast pipeline stays open between samples
and keeps converting and scaling while the shared monitor changes, even when
no sample is due; on X11 every sample copies the monitor region out of the X
server. The window labels are queried at most every 3 s while sampling (EWMH
on X11; on KDE Wayland the tray loads, starts and unloads a small script in
the compositor and waits on a nested main loop). Everything shares the tray's
single GTK thread, so a sample runs between two interface updates: the
defaults stay in the low tens of milliseconds, but a large `ghost.width`
makes a sample tens to hundreds of milliseconds and a visible hitch.

The same steps measured on the maintainer's Wayland machine (Fedora KDE,
Python 3.14, the same panel rotated 90° and scaled 1.75, so the model is
480×640):

| Step | Cost |
| --- | --- |
| open the portal session and take the first frame | 0.12 s with the stored grant, no dialog |
| ScreenCast pipeline open but idle | 0.1% of one core, ~51 MB RSS |
| frame pull | 1–30 ms when a new frame is available; up to ~1 s while the panel does not change, because the stream is damage-driven |
| model update, 480×640 | 19 ms with ~10% of the pixels changed, 46 ms on a whole-frame change |
| the whole tray process, estimate and auto-clearing active | ~100 MB RSS, ~1.7% CPU over a session |

The Wayland pipeline only produces frames on damage, so an idle panel is
nearly free, and the open `Ghost estimate…` window (or an auto-clear wave)
keeps pulling them at the base cadence instead.

## Known limitations

- Both polarities are estimated: dark residue on light content and light
  residue on dark content. Light areas also cover dark ink that was just
  drawn and is not fully saturated, so they must be judged on the panel;
  `noise`, `min_error`, `gamma_ink` and `gamma_erase` are assumptions, not
  yet configurable (`threshold` is). Compare the estimate with the panel
  over a few sessions and tune them.
- On Wayland the clearing overlay needs layer-shell: KDE and wlroots
  compositors provide it, GNOME does not and the feature reports itself
  unavailable. Capture works through the portal everywhere; the window
  labels need KDE's KWin scripting.
- The monitor-presence check on Wayland reads `/sys/class/drm`; without
  readable connected outputs it returns "cannot tell" and only the serial
  exchanges decide, as on X11 without the EDID names.

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
  `org.dasungctl.Zones`. The desktop-move action
  (`windows.KWIN_MOVE_SCRIPT`) reuses the same file and plugin and matches
  the `dasungctl` resource class, which GTK derives from the program name on
  Wayland (`GLib.set_prgname` in the tray startup).
- A Wayland overlay must contain a drawing widget: a bare `Gtk.Window` on a
  layer surface never attaches a buffer, so the surface stays invisible.
  `zoneclear.WaylandZoneFlasher` paints the phase colour in a
  `Gtk.DrawingArea`.
- The Wayland panel-presence check reads the connected outputs and their
  EDID names in `/sys/class/drm`; the entry names drop the `cardN-` prefix
  and match the compositor's output names (`DP-1`).
- *The model* section above describes the formulas; keep it in sync with
  `ghostwatch.py`. Components are built per polarity, and the label is
  recorded when a cell first becomes dirty and kept while it stays dirty.
- Sampling backs off by doubling up to `max_interval` while samples show no
  changes; `set_force_base(True)` (the open window) restores the base rate.
- With a static screen, frames differ only by capture jitter below the
  per-pixel noise floor, so `changed_pixels` can legitimately stay 0 for many
  samples.

The physical measurements that led here — refresh efficacy, the removed grid
and adaptive clearing modes, the rejected low-amplitude maintenance — were
taken during development.
