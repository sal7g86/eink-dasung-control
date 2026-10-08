"""Screen capture backends for the ghost watcher.

X11 reads the Dasung monitor's region of the root window: an XGetImage of
the rectangle through python-xlib when it is installed (cheap), the GTK
root-window grab as the fallback. KDE Wayland goes through the ScreenCast
portal and reads the PipeWire stream with GStreamer: KWin's own ScreenShot2
API is allowlisted to installed screenshot applications, so a script cannot
use it. Both backends import ``gi`` lazily, so this module stays importable
in the project virtualenv where the GTK bindings do not exist.

No image is ever written to disk: each backend returns grayscale bytes at
the model resolution, with the coordinates of the captured region so the
elements can be reported in screen pixels.
"""

from __future__ import annotations

from fractions import Fraction
import json
import os
from pathlib import Path
import sys
import threading
import time

from . import paths
from .ghostwatch import CaptureError, Frame
from .panels import get_panel


# The portal waits for the user to pick a monitor the first time.
SESSION_TIMEOUT_MS = 120_000
# A partial X11 read stops paying off when the blocks to re-read cover more
# than this fraction of the model frame: the whole region is read instead.
PARTIAL_MAX_FRACTION = 0.5
# Damage rectangles kept apart before they are merged into one bounding box.
MAX_DAMAGE_RECTS = 8
# Heartbeat check of the X11 damage tracking: a full read that finds this
# fraction of the model pixels changed (beyond the noise) although no damage
# was reported means the tracking misses updates; two such reads in a row
# turn it off for the session.
DAMAGE_MISS_FRACTION = 0.005
DAMAGE_MISS_NOISE = 10
DAMAGE_MISS_LIMIT = 2
# Window maps, unmaps and moves also repaint the shadow the compositor draws
# around the window, which no X damage reports: widen those rectangles.
SHADOW_MARGIN = 64
# Host applications register a stable application id so the portal can keep
# permissions (including the ScreenCast restore token) across runs.
APP_ID = "dasungctl"


class PortalRefused(CaptureError):
    """The portal answered a request with a non-zero response code."""

    def __init__(self, message: str, code: int) -> None:
        """Keep the portal response code for the caller's message."""

        super().__init__(message)
        self.code = code


def scale_dimensions(
    source_width: int, source_height: int, width: int
) -> tuple[int, int]:
    """Target size with the source aspect ratio, at the requested width."""

    if width <= 0 or source_width <= 0 or source_height <= 0:
        raise ValueError("sizes must be positive")
    height = max(1, round(source_height * width / source_width))
    return width, height


def gray_from_channels(
    data: bytes,
    width: int,
    height: int,
    channels: int,
    stride: int | None = None,
) -> bytes:
    """Reduce interleaved RGB/RGBA/GRAY pixels to one gray byte per pixel."""

    if width <= 0 or height <= 0:
        raise ValueError("sizes must be positive")
    if channels not in (1, 3, 4):
        raise ValueError("channels must be 1, 3 or 4")
    row_bytes = width * channels
    stride = row_bytes if stride is None else stride
    if stride < row_bytes:
        raise ValueError("stride is smaller than one row")
    gray = bytearray(width * height)
    index = 0
    for y in range(height):
        row = y * stride
        for x in range(width):
            source = row + x * channels
            if channels == 1:
                gray[index] = data[source]
            else:
                gray[index] = (
                    data[source] + 2 * data[source + 1] + data[source + 2]
                ) >> 2
            index += 1
    return bytes(gray)


def gray_from_pixbuf(pixbuf) -> bytes:
    """One gray byte per pixel of a GdkPixbuf, with the per-pixel work in C.

    ``saturate_and_pixelate`` with saturation 0 turns the pixbuf grey in
    place (luminance weights), then one channel per row is sliced out.
    The pure-Python `gray_from_channels` loop cost about 18 ms per sample at
    the model size; this route costs about 1 ms. The pixbuf is modified, so
    callers pass a scratch copy (the scaled frame).
    """

    width = pixbuf.get_width()
    height = pixbuf.get_height()
    channels = pixbuf.get_n_channels()
    stride = pixbuf.get_rowstride()
    if width <= 0 or height <= 0 or channels not in (3, 4):
        raise ValueError("unsupported pixbuf layout")
    pixbuf.saturate_and_pixelate(pixbuf, 0.0, False)
    pixels = pixbuf.get_pixels()
    row = width * channels
    return b"".join(
        pixels[y * stride : y * stride + row : channels] for y in range(height)
    )


def merge_rects(
    rects: list[tuple[int, int, int, int]], limit: int = MAX_DAMAGE_RECTS
) -> list[tuple[int, int, int, int]]:
    """Merge overlapping ``(x0, y0, x1, y1)`` rectangles, at most `limit`.

    Overlapping or touching rectangles become their bounding box; past the
    limit everything collapses into one box, which keeps the partial reads
    few while two distant small changes still cost two small reads.
    """

    merged: list[list[int]] = []
    for x0, y0, x1, y1 in rects:
        if x1 <= x0 or y1 <= y0:
            continue
        box = [x0, y0, x1, y1]
        changed = True
        while changed:
            changed = False
            for other in merged:
                if (
                    box[0] <= other[2]
                    and other[0] <= box[2]
                    and box[1] <= other[3]
                    and other[1] <= box[3]
                ):
                    box = [
                        min(box[0], other[0]),
                        min(box[1], other[1]),
                        max(box[2], other[2]),
                        max(box[3], other[3]),
                    ]
                    merged.remove(other)
                    changed = True
                    break
        merged.append(box)
    if len(merged) > limit:
        merged = [
            [
                min(box[0] for box in merged),
                min(box[1] for box in merged),
                max(box[2] for box in merged),
                max(box[3] for box in merged),
            ]
        ]
    return [tuple(box) for box in merged]


def partial_plan(
    dirty: tuple[int, int, int, int],
    device_size: tuple[int, int],
    model_size: tuple[int, int],
):
    """Blocks to re-read for one damaged device rectangle.

    The model frame is a fixed reduction of the device region, and
    ``model / device = p / q`` in lowest terms: every q device pixels reduce
    onto exactly p model pixels. A read that starts and ends on those blocks
    therefore reduces to the very same model pixels as the whole frame. The
    damage is widened by one model pixel for the filter footprint, then to
    whole blocks with at least one more model pixel of slack, and only the
    model pixels covering the damage are copied back: checked byte for byte
    against a full GdkPixbuf BILINEAR reduction (a block edge that touches
    the copied pixels drifts by one level, one pixel of slack removes it).

    Returns ``(source, block, inner)``, the device rectangle to read, the
    model rectangle it reduces to and the model rectangle to copy, each as
    ``(x0, y0, x1, y1)``; None when the damage misses the frame.
    """

    x0, y0, x1, y1 = dirty
    device_width, device_height = device_size
    width, height = model_size
    ratio_x = Fraction(width, device_width)
    ratio_y = Fraction(height, device_height)
    block_x, source_x = ratio_x.numerator, ratio_x.denominator
    block_y, source_y = ratio_y.numerator, ratio_y.denominator
    inner_x0 = max(0, x0 * width // device_width - 1)
    inner_y0 = max(0, y0 * height // device_height - 1)
    inner_x1 = min(width, -(-x1 * width // device_width) + 1)
    inner_y1 = min(height, -(-y1 * height // device_height) + 1)
    if inner_x1 <= inner_x0 or inner_y1 <= inner_y0:
        return None
    block_x0 = max(0, (inner_x0 - 1) // block_x * block_x)
    block_y0 = max(0, (inner_y0 - 1) // block_y * block_y)
    block_x1 = min(width, -(-(inner_x1 + 1) // block_x) * block_x)
    block_y1 = min(height, -(-(inner_y1 + 1) // block_y) * block_y)
    source = (
        block_x0 // block_x * source_x,
        block_y0 // block_y * source_y,
        block_x1 // block_x * source_x,
        block_y1 // block_y * source_y,
    )
    return (
        source,
        (block_x0, block_y0, block_x1, block_y1),
        (inner_x0, inner_y0, inner_x1, inner_y1),
    )


class X11DamageTracker:
    """Changed screen areas from XDamage on the top-level windows.

    The root window is no use under a compositing manager: Muffin reported
    the whole screen for every repaint, about 17 times a second. A damage
    object on each root child (the WM frames and override-redirect windows)
    reports the exact rectangles a window redraws, for example one terminal
    line, the same way compositors track their windows. Maps, unmaps and
    configures (moves, resizes, restacks) mark both the old and the new
    rectangle, widened by the compositor's shadow. What the compositor
    draws by itself (Cinnamon's panels, notifications and OSDs) has no X
    window and reports no damage: the watcher's periodic full read picks
    it up. Everything is in root (device) coordinates, clipped to the
    tracked monitor. The tracker lives on the capture's own python-xlib
    connection and is drained from the tray timer, so no thread and no
    main-loop source are involved.
    """

    def __init__(self, display, clip: tuple[int, int, int, int]) -> None:
        """Start tracking; raises CaptureError when DAMAGE is unavailable."""

        from Xlib import X
        from Xlib.ext import damage

        if not display.has_extension("DAMAGE"):
            raise CaptureError("the X server has no DAMAGE extension")
        # The protocol requires the version handshake before DamageCreate.
        display.damage_query_version()
        self._display = display
        self._X = X
        self._damage = damage
        self._root = display.screen().root
        self._clip = clip
        self._windows: dict[int, tuple[int, int, int, int]] = {}
        self._damages: dict[int, int] = {}
        self._rects: list[tuple[int, int, int, int]] = []
        # Windows vanish between the query and the request all the time;
        # their asynchronous errors are expected and must not be printed.
        display.set_error_handler(lambda *_args: None)
        self._root.change_attributes(event_mask=X.SubstructureNotifyMask)
        for window in self._root.query_tree().children:
            self._watch(window, mark=False)
        display.flush()

    def set_clip(self, clip: tuple[int, int, int, int]) -> None:
        """Follow a monitor that moved or changed size."""

        self._clip = clip

    def _watch(self, window, *, mark: bool = True) -> None:
        if window.id in self._windows:
            if mark:
                self._mark(self._windows[window.id], SHADOW_MARGIN)
            return
        try:
            if window.get_attributes().map_state != self._X.IsViewable:
                return
            geometry = window.get_geometry()
            damage = window.damage_create(self._damage.DamageReportBoundingBox)
        except Exception:  # BadWindow/BadDrawable races with destruction
            return
        self._damages[window.id] = damage
        border = 2 * geometry.border_width
        rect = (
            geometry.x,
            geometry.y,
            geometry.width + border,
            geometry.height + border,
        )
        self._windows[window.id] = rect
        if mark:
            self._mark(rect, SHADOW_MARGIN)

    def _mark(self, rect: tuple[int, int, int, int], margin: int = 0) -> None:
        """Add a root rectangle ``(x, y, width, height)``, clipped."""

        x, y, width, height = rect
        x -= margin
        y -= margin
        width += 2 * margin
        height += 2 * margin
        clip_x, clip_y, clip_width, clip_height = self._clip
        x0 = max(x, clip_x)
        y0 = max(y, clip_y)
        x1 = min(x + width, clip_x + clip_width)
        y1 = min(y + height, clip_y + clip_height)
        if x1 <= x0 or y1 <= y0:
            return
        self._rects.append((x0, y0, x1, y1))
        if len(self._rects) > 4 * MAX_DAMAGE_RECTS:
            self._rects = merge_rects(self._rects)

    def drain(self) -> None:
        """Process the queued events without blocking."""

        X = self._X
        display = self._display
        # python-xlib clones extension event classes per display, so an
        # isinstance check against `damage.DamageNotify` never matches:
        # compare the registered event code instead.
        notify = display.extension_event.DamageNotify
        while display.pending_events():
            event = display.next_event()
            if event.type == notify:
                origin = event.drawable_geometry
                area = event.area
                self._mark(
                    (origin.x + area.x, origin.y + area.y, area.width, area.height)
                )
                display.damage_subtract(event.damage)
            elif event.type == X.MapNotify:
                self._watch(event.window)
            elif event.type in (X.UnmapNotify, X.DestroyNotify):
                rect = self._windows.get(event.window.id)
                if rect is not None:
                    self._mark(rect, SHADOW_MARGIN)
                if event.type == X.DestroyNotify:
                    # The server frees the damage object with its drawable.
                    self._windows.pop(event.window.id, None)
                    self._damages.pop(event.window.id, None)
            elif event.type == X.ReparentNotify:
                if event.parent.id == self._root.id:
                    self._watch(event.window)
                else:
                    # Now inside a WM frame: its damage would be reported
                    # relative to the frame, and the frame is tracked.
                    self._forget(event.window.id)
            elif event.type == X.ConfigureNotify:
                old = self._windows.get(event.window.id)
                border = 2 * event.border_width
                new = (event.x, event.y, event.width + border, event.height + border)
                if old is not None:
                    self._mark(old, SHADOW_MARGIN)
                    self._windows[event.window.id] = new
                # A restack changes what is visible even without a move.
                self._mark(new, SHADOW_MARGIN)
        display.flush()

    def _forget(self, window_id: int) -> None:
        rect = self._windows.pop(window_id, None)
        if rect is not None:
            self._mark(rect, SHADOW_MARGIN)
        damage = self._damages.pop(window_id, None)
        if damage is not None:
            try:
                self._display.damage_destroy(damage)
            except Exception:  # pragma: no cover - the window may be gone
                pass

    def close(self) -> None:
        """Destroy the damage objects (the connection itself stays)."""

        for window_id in list(self._damages):
            damage = self._damages.pop(window_id)
            try:
                self._display.damage_destroy(damage)
            except Exception:  # pragma: no cover - teardown must not fail
                pass
        self._windows.clear()
        try:
            self._display.flush()
        except Exception:  # pragma: no cover - teardown must not fail
            pass

    @property
    def pending(self) -> bool:
        """True when some damage is waiting for the next read."""

        return bool(self._rects)

    def take(self) -> list[tuple[int, int, int, int]]:
        """The merged damaged rectangles since the last call, then forget."""

        rects = merge_rects(self._rects)
        self._rects = []
        return rects


def x_image_rgb(
    data, width: int, height: int, depth: int, stride: int
) -> bytes | None:
    """RGB bytes from an XGetImage reply, or None for an unsupported format.

    XGetImage answers in the server's visual: the common little-endian
    24/32-bpp layout stores each pixel as ``B G R X``. Anything else is
    left to the GTK grab, which is slower but works everywhere.
    """

    if sys.byteorder != "little" or depth not in (24, 32):
        return None
    if width <= 0 or height <= 0 or stride < width * 4:
        return None
    if len(data) < stride * height:
        return None
    rgb = bytearray(width * height * 3)
    view = memoryview(data)
    for row in range(height):
        source = view[row * stride : row * stride + width * 4]
        base = row * width * 3
        rgb[base : base + 3 * width : 3] = source[2::4]
        rgb[base + 1 : base + 3 * width : 3] = source[1::4]
        rgb[base + 2 : base + 3 * width : 3] = source[0::4]
    return bytes(rgb)


def pick_monitor(
    monitors, wanted: str = "auto", extra_names=None, panel_names=None
):
    """First monitor whose model matches one of the wanted names.

    `monitors` is a sequence of objects with ``get_model()``. With `wanted`
    set to `auto` the names of the shipped panel profiles are tried
    (`Paperlike` by default, so other models are found once their profile
    lists its EDID name); any other string is a case-insensitive substring
    of the monitor name. The optional ``extra_names`` is a sequence parallel
    to ``monitors``, each item an iterable of additional names to match: on
    X11 Gdk reports the RandR output name (`DP-1`), while the EDID carries
    the real model (`Paperlike H D`). A single monitor is used when nothing
    matches, so a one-screen machine still works; otherwise the user has to
    name the model in `ghost.output`.
    """

    if wanted in (None, "", "auto"):
        needles = tuple(panel_names) if panel_names else ("paperlike",)
    else:
        needles = (wanted,)
    needles = tuple(
        str(needle).strip().lower() for needle in needles if str(needle).strip()
    )
    for index, monitor in enumerate(monitors):
        names = [monitor.get_model() or ""]
        if extra_names is not None and index < len(extra_names):
            names.extend(extra_names[index])
        for name in names:
            text = str(name).lower()
            if any(needle in text for needle in needles):
                return monitor
    if len(monitors) == 1:
        return monitors[0]
    return None


def edid_monitor_name(edid: bytes) -> str | None:
    """Monitor name from the EDID 0xFC descriptor, or None when absent.

    The name sits in one of the four 18-byte descriptor blocks starting at
    byte 54; many monitors end it with a newline or pad it with spaces.
    """

    data = bytes(edid)
    if len(data) < 128:
        return None
    for offset in (54, 72, 90, 108):
        block = data[offset : offset + 18]
        if block[:4] != b"\x00\x00\x00\xfc":
            continue
        name = block[5:18].split(b"\n", 1)[0].decode("latin-1").strip()
        if name:
            return name
    return None


# DRM connector entries; readable without the compositor, used on Wayland
# where Gdk does not expose the outputs' EDID names.
DRM_DIR = Path("/sys/class/drm")


def drm_output_names(root: Path | None = None) -> dict[str, str | None]:
    """Connected DRM outputs as name -> EDID model name (None when absent).

    Names drop the `cardN-` prefix so they read like the compositor's output
    names (`DP-1`, `HDMI-A-3`). Disconnected connectors are skipped: the
    panel's receiver disappears when it is switched off, which is exactly the
    signal the availability check needs.
    """

    base = Path(root) if root is not None else DRM_DIR
    try:
        entries = sorted(base.glob("card*-*"))
    except OSError:  # pragma: no cover - depends on the host
        return {}
    outputs: dict[str, str | None] = {}
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if status != "connected":
            continue
        name = entry.name.split("-", 1)[1] if "-" in entry.name else entry.name
        model: str | None = None
        try:
            edid = (entry / "edid").read_bytes()
        except OSError:
            edid = b""
        if edid:
            model = edid_monitor_name(edid)
        outputs[name] = model
    return outputs


def drm_output_present(
    wanted: str = "auto", panel_names=None, root: Path | None = None
) -> bool | None:
    """Whether a connected DRM output is the e-ink panel (Wayland check).

    Matching is the capture's: the EDID model names of the active panel
    profile (`auto`) or the configured `ghost.output` substring, against the
    output name or its EDID model. Returns None when the check cannot tell
    (no readable DRM entries, or `auto` without any EDID name), so the caller
    keeps the serial-only behaviour; it never falls back to "the only
    monitor", which would mistake the desktop screen for the panel.
    """

    outputs = drm_output_names(root)
    if not outputs:
        return None
    if wanted in (None, "", "auto"):
        needles = tuple(panel_names) if panel_names else ("paperlike",)
        if not any(model for model in outputs.values()):
            return None
    else:
        needles = (wanted,)
    needles = tuple(
        str(needle).strip().lower() for needle in needles if str(needle).strip()
    )
    for name, model in outputs.items():
        for candidate in (name, model or ""):
            if any(needle in candidate.lower() for needle in needles):
                return True
    return False


def x11_edid_monitor_names() -> dict[tuple[int, int, int, int], str]:
    """EDID monitor names by RandR output geometry (best effort).

    This is what lets `auto` find the Dasung on X11 with several monitors:
    Gdk only exposes the output name there, and the EDID property carries
    the model. Returns an empty mapping when python-xlib is unavailable or
    the X server does not expose the property; the caller falls back to the
    Gdk names.
    """

    try:
        from Xlib import X
        from Xlib import display as xdisplay
        from Xlib.ext import randr
    except Exception:
        return {}
    try:
        connection = xdisplay.Display()
        root = connection.screen().root
        resources = randr.get_screen_resources_current(root)
        names: dict[tuple[int, int, int, int], str] = {}
        for output in resources.outputs:
            info = randr.get_output_info(
                connection, output, resources.config_timestamp
            )
            if not info.crtc:
                continue
            crtc = randr.get_crtc_info(
                connection, info.crtc, resources.config_timestamp
            )
            try:
                prop = randr.get_output_property(
                    connection,
                    output,
                    connection.intern_atom("EDID"),
                    X.AnyPropertyType,
                    0,
                    1024,
                )
            except Exception:
                continue
            if prop is None:
                continue
            name = edid_monitor_name(bytes(prop.value))
            if name:
                names[(crtc.x, crtc.y, crtc.width, crtc.height)] = name
        return names
    except Exception:  # pragma: no cover - depends on the X server
        return {}


def x11_device_geometry(monitor) -> tuple[int, int, int, int]:
    """Gdk monitor geometry in X root coordinates (device pixels).

    Gdk reports monitor geometry in application pixels while the RandR
    output rectangle is in device pixels, and `x11_edid_monitor_names` keys
    its names by it. The window scale factor bridges the two: an unscaled
    session returns the geometry unchanged, an integer-scaled X11 session
    (Cinnamon's fractional scaling in `scale-ui-down` mode, a doubled KDE
    UI) multiplies it.
    """

    geometry = monitor.get_geometry()
    getter = getattr(monitor, "get_scale_factor", None)
    scale = 1
    if callable(getter):
        try:
            scale = max(1, int(getter()))
        except (TypeError, ValueError):  # pragma: no cover - odd Gdk build
            scale = 1
    if scale == 1:
        return geometry.x, geometry.y, geometry.width, geometry.height
    return (
        geometry.x * scale,
        geometry.y * scale,
        geometry.width * scale,
        geometry.height * scale,
    )


def x11_monitor_aliases(monitors) -> list[tuple[str, ...]]:
    """EDID names for the Gdk monitors, matched by RandR geometry.

    The returned list is parallel to `monitors`; an empty tuple means no
    EDID match (output unplugged, xlib missing).
    """

    edid_names = x11_edid_monitor_names()
    if not edid_names:
        return []
    aliases: list[tuple[str, ...]] = []
    for monitor in monitors:
        name = edid_names.get(x11_device_geometry(monitor))
        aliases.append((name,) if name else ())
    return aliases


def monitor_output_present(
    gdk, wanted: str = "auto", panel_names=None
) -> bool | None:
    """Whether the e-ink display output is currently present.

    The panel's HDMI receiver disappears when it is switched off, while the
    CH340 stays powered and its serial selectors keep answering the stored
    values: the output is the confirmed signal that tells "panel off" from
    "panel on". On X11 the matching runs over the Gdk monitors and their EDID
    names (through python-xlib); on Wayland over the DRM sysfs entries of the
    connected outputs, which carry the same EDID model names. The matching is
    the capture's: the EDID model names of the panel profiles and the
    configured `ghost.output`. Returns None when the check cannot tell (no
    display, no readable DRM data, or `auto` without EDID names), so the
    caller keeps the serial-only behaviour; it never falls back to "the only
    monitor", which would mistake the desktop screen for the panel.
    """

    if os.environ.get("WAYLAND_DISPLAY"):
        return drm_output_present(wanted, panel_names)
    display = gdk.Display.get_default()
    if display is None:
        return None
    monitors = [
        display.get_monitor(index) for index in range(display.get_n_monitors())
    ]
    if not monitors:
        return None
    aliases = x11_monitor_aliases(monitors)
    if wanted in (None, "", "auto"):
        needles = tuple(panel_names) if panel_names else ("paperlike",)
        if not aliases:
            # Gdk only reports the output name (DP-1, HDMI-0) on X11; without
            # the EDID names the model cannot be identified.
            return None
    else:
        needles = (wanted,)
    needles = tuple(
        str(needle).strip().lower() for needle in needles if str(needle).strip()
    )
    for index, monitor in enumerate(monitors):
        names = [monitor.get_model() or ""]
        if index < len(aliases):
            names.extend(aliases[index])
        if any(
            needle in str(name).lower() for name in names for needle in needles
        ):
            return True
    return False


def load_restore_token(path: Path | None = None) -> str | None:
    """Read the saved ScreenCast restore token; a broken file means none."""

    target = path or paths.screencast_path()
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    token = data.get("restore_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def save_restore_token(token: str, path: Path | None = None) -> Path:
    """Remember the granted token so later sessions do not ask again."""

    target = path or paths.screencast_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"restore_token": token}) + "\n", encoding="utf-8"
    )
    return target


class X11Capture:
    """Grab the Dasung region of the X11 root window.

    The pixels come from an XGetImage of the region through python-xlib
    when it is installed; the GTK grab stays as the fallback. With
    python-xlib and the DAMAGE extension the capture also knows what
    changed: `poll_changes` tells the watcher whether a sample is worth
    taking, and a sample re-reads only the damaged blocks of the monitor
    into the cached model frame (see `partial_plan`).
    """

    name = "x11"

    def __init__(
        self, gdk, pixbuf, wanted: str = "auto", panel_names=None
    ) -> None:
        """`gdk`/`pixbuf` are the GTK modules; `wanted` names the monitor.

        `panel_names` are the EDID/model substrings of the active panel
        profile, used when `wanted` is `auto`.
        """

        self._gdk = gdk
        self._pixbuf = pixbuf
        self._wanted = wanted
        self._panel_names = tuple(panel_names or ("paperlike",))
        self._monitor = None
        # python-xlib connection for the direct reads (None until needed).
        self._xdisplay = None
        # Global origin of the captured region (None until the first grab);
        # the zone provider needs it to convert window rectangles.
        self.source_origin: tuple[int, int] | None = None
        # Change tracking: the damage tracker (None until the first full
        # read, or when unavailable), whether it may still be tried, the
        # cached model frame with the geometry it belongs to, and the
        # heartbeat verification state.
        self._tracker: X11DamageTracker | None = None
        self._tracking_allowed = True
        self._gray: bytearray | None = None
        self._frame_key = None
        self._full_next = False
        self._misses = 0
        # One-line explanation when the tracking was turned off, for the log.
        self.notice: str | None = None

    def reset(self) -> None:
        """Resolve the monitor again on the next grab."""

        self._monitor = None
        self._full_next = True

    def close(self) -> None:
        """Stop the change tracking (the tray calls this at exit)."""

        self._drop_tracker()

    def _drop_tracker(self) -> None:
        if self._tracker is not None:
            self._tracker.close()
        self._tracker = None

    def _stop_tracking(self, reason: str) -> None:
        """Fall back to polling for the rest of the session."""

        self._drop_tracker()
        self._tracking_allowed = False
        self.notice = f"X11 change tracking off ({reason}); sampling by timer"

    # -- change tracking ---------------------------------------------------

    def poll_changes(self) -> bool | None:
        """Whether the monitor changed since the last grab.

        None means this capture cannot tell (no python-xlib, no DAMAGE, not
        started yet, or turned off) and the watcher keeps its timer.
        """

        tracker = self._tracker
        if tracker is None:
            return None
        try:
            tracker.drain()
        except Exception as exc:  # pragma: no cover - X connection trouble
            self._stop_tracking(f"event error: {exc}")
            return None
        return tracker.pending

    def request_full(self) -> None:
        """Read the whole region next time and check the tracking with it."""

        self._full_next = True

    def _start_tracker(self, clip: tuple[int, int, int, int]) -> None:
        if not self._tracking_allowed or self._xdisplay is None:
            return
        if self._tracker is not None:
            self._tracker.set_clip(clip)
            return
        try:
            self._tracker = X11DamageTracker(self._xdisplay, clip)
        except Exception as exc:
            self._stop_tracking(str(exc) or type(exc).__name__)

    def _xlib_pixbuf(self, x: int, y: int, width: int, height: int):
        """The device rectangle as a pixbuf through XGetImage, or None.

        `Gdk.pixbuf_get_from_window` reads through the whole scaled desktop
        and costs about a second of X server CPU per sample whatever
        rectangle is asked for; a direct XGetImage of the region returns
        the same pixels in a fraction of that. python-xlib is an optional
        dependency, so a missing binding or a failing read falls back to
        the GTK route.
        """

        try:
            from Xlib import X
            from Xlib import display as xdisplay
        except Exception:  # pragma: no cover - depends on the host
            return None
        if self._xdisplay is None:
            try:
                self._xdisplay = xdisplay.Display()
            except Exception:  # pragma: no cover - depends on the X server
                return None
        try:
            image = self._xdisplay.screen().root.get_image(
                x, y, width, height, X.ZPixmap, 0xFFFFFFFF
            )
            rgb = x_image_rgb(
                image.data, width, height, image.depth, len(image.data) // height
            )
        except Exception:  # pragma: no cover - depends on the X server
            self._xdisplay = None
            return None
        if rgb is None:
            return None
        try:
            from gi.repository import GLib

            return self._pixbuf.Pixbuf.new_from_bytes(
                GLib.Bytes.new(rgb),
                self._pixbuf.Colorspace.RGB,
                False,
                8,
                width,
                height,
                width * 3,
            )
        except Exception:  # pragma: no cover - depends on the bindings
            return None

    def _resolve(self):
        if self._monitor is not None:
            return self._monitor
        display = self._gdk.Display.get_default()
        if display is None:
            raise CaptureError("no X11 display available")
        monitors = [
            display.get_monitor(index) for index in range(display.get_n_monitors())
        ]
        aliases = x11_monitor_aliases(monitors)
        monitor = pick_monitor(
            monitors, self._wanted, aliases, self._panel_names
        )
        if monitor is None:
            labels = []
            for index, item in enumerate(monitors):
                model = item.get_model() or "?"
                alias = aliases[index][0] if index < len(aliases) and aliases[index] else ""
                if alias and alias.lower() != model.lower():
                    model = f"{model} ({alias})"
                labels.append(model)
            raise CaptureError(
                f"Dasung monitor not found (models: {', '.join(labels)}); "
                "set ghost.output to part of the monitor name"
            )
        self._monitor = monitor
        return monitor

    def grab(self, width: int) -> Frame:
        """Grab the resolved monitor, reduced to `width` model columns.

        With change tracking active and a cached frame of the same geometry
        only the damaged blocks are read again; otherwise, and on the
        watcher's heartbeat, the whole region is read.
        """

        monitor = self._resolve()
        geometry = monitor.get_geometry()
        if geometry.width <= 0 or geometry.height <= 0:
            raise CaptureError("the monitor has no usable geometry")
        device = x11_device_geometry(monitor)
        target_width, target_height = scale_dimensions(
            geometry.width, geometry.height, width
        )
        key = (device, target_width, target_height)
        tracker = self._tracker
        if tracker is not None and self._xdisplay is not tracker._display:
            # The xlib connection was replaced after a failed read.
            self._drop_tracker()
            tracker = None
        partial = (
            tracker is not None
            and self._gray is not None
            and key == self._frame_key
            and not self._full_next
        )
        if tracker is not None:
            try:
                tracker.drain()
            except Exception as exc:  # pragma: no cover - X trouble
                self._stop_tracking(f"event error: {exc}")
                tracker = None
                partial = False
        # Take the damage before reading: whatever changes during the read
        # stays queued for the next sample instead of being lost.
        rects = tracker.take() if tracker is not None else []
        gray = None
        if partial:
            gray = self._read_partial(device, target_width, target_height, rects)
        if gray is None:
            gray = self._read_full(geometry, device, target_width, target_height)
            if tracker is not None and self._full_next:
                self._verify_tracking(gray, key, rects)
            self._full_next = False
            self._frame_key = key
            self._start_tracker(device)
        self._gray = gray
        self.source_origin = (geometry.x, geometry.y)
        return Frame(
            gray=bytes(gray),
            width=target_width,
            height=target_height,
            source_width=geometry.width,
            source_height=geometry.height,
        )

    def _verify_tracking(self, gray: bytearray, key, rects) -> None:
        """Heartbeat check: a quiet tracker must mean an unchanged frame."""

        previous = self._gray
        if rects or previous is None or key != self._frame_key:
            self._misses = 0
            return
        # Damage that arrived while the region was being read explains a
        # difference just as well: such a read proves nothing either way.
        tracker = self._tracker
        assert tracker is not None
        tracker.drain()
        if tracker.pending:
            return
        differing = sum(
            1
            for old, new in zip(previous, gray)
            if old - new > DAMAGE_MISS_NOISE or new - old > DAMAGE_MISS_NOISE
        )
        if differing > DAMAGE_MISS_FRACTION * len(gray):
            self._misses += 1
            if self._misses >= DAMAGE_MISS_LIMIT:
                self._stop_tracking("updates were not reported")
        else:
            self._misses = 0

    def _scaled_gray(self, pixbuf, width: int, height: int) -> bytes:
        small = pixbuf.scale_simple(
            width, height, self._pixbuf.InterpType.BILINEAR
        )
        if small is None:
            raise CaptureError("cannot scale the captured frame")
        try:
            return gray_from_pixbuf(small)
        except Exception:  # pragma: no cover - depends on the bindings
            return gray_from_channels(
                small.get_pixels(),
                small.get_width(),
                small.get_height(),
                small.get_n_channels(),
                small.get_rowstride(),
            )

    def _read_full(self, geometry, device, width: int, height: int) -> bytearray:
        pixbuf = self._xlib_pixbuf(*device)
        if pixbuf is None:
            root = self._gdk.get_default_root_window()
            pixbuf = self._gdk.pixbuf_get_from_window(
                root,
                geometry.x,
                geometry.y,
                geometry.width,
                geometry.height,
            )
        if pixbuf is None:
            raise CaptureError("X11 could not capture the monitor region")
        return bytearray(self._scaled_gray(pixbuf, width, height))

    def _read_partial(
        self, device, width: int, height: int, rects
    ) -> bytearray | None:
        """Re-read only the damaged blocks; None asks for a full read."""

        gray = self._gray
        assert gray is not None
        if not rects:
            return gray
        device_x, device_y, device_width, device_height = device
        plans = []
        area = 0
        for x0, y0, x1, y1 in rects:
            plan = partial_plan(
                (x0 - device_x, y0 - device_y, x1 - device_x, y1 - device_y),
                (device_width, device_height),
                (width, height),
            )
            if plan is None:
                continue
            block = plan[1]
            area += (block[2] - block[0]) * (block[3] - block[1])
            plans.append(plan)
        if area > PARTIAL_MAX_FRACTION * width * height:
            return None
        for source, block, inner in plans:
            pixbuf = self._xlib_pixbuf(
                device_x + source[0],
                device_y + source[1],
                source[2] - source[0],
                source[3] - source[1],
            )
            if pixbuf is None:
                return None
            block_width = block[2] - block[0]
            block_gray = self._scaled_gray(
                pixbuf, block_width, block[3] - block[1]
            )
            left = inner[0] - block[0]
            span = inner[2] - inner[0]
            for row in range(inner[1], inner[3]):
                at = (row - block[1]) * block_width + left
                gray[row * width + inner[0] : row * width + inner[2]] = (
                    block_gray[at : at + span]
                )
        return gray


class WaylandCapture:
    """ScreenCast portal session plus two small GStreamer pipelines.

    The portal dialog appears the first time and asks which screen to share;
    with persist mode the returned restore token makes later sessions silent.
    The stream stays open between samples and stays raw: a `new-sample`
    callback only keeps the latest frame and notes that the screen changed
    (the compositor sends frames only then), so nothing is converted while
    nobody samples. A sample converts that one frame to GRAY8 at the model
    size in a second pipeline. The old single pipeline converted and scaled
    every frame at full resolution, up to the panel's refresh rate, to keep
    one every few seconds.
    """

    name = "screencast"

    def __init__(
        self, restore_token: str | None = None, *, timeout_ms: int = SESSION_TIMEOUT_MS
    ) -> None:
        """Prepare the portal client; nothing opens until the first grab."""

        self._restore_token = restore_token or load_restore_token()
        self._timeout_ms = timeout_ms
        self._gio = None
        self._glib = None
        self._gst = None
        self._bus = None
        self._session = None
        self._pipeline = None
        self._sink = None
        self._frame_size = None
        self._source = None
        self._node = None
        self._request_count = 0
        # Latest raw frame from the stream thread, whether it arrived after
        # the last grab, and a signal for the first one.
        self._lock = threading.Lock()
        self._latest = None
        self._fresh = False
        self._arrived = threading.Event()
        # The on-demand GRAY8 conversion and the last frame it converted.
        self._converter = None
        self._converter_size = None
        self._converted = None
        self._converted_gray: bytes | None = None
        # Global origin of the shared output, from the portal properties.
        self._origin: tuple[int, int] | None = None
        self.source_origin: tuple[int, int] | None = None

    @property
    def restore_token(self) -> str | None:
        """The token that lets the next start reuse the granted share."""

        return self._restore_token

    def reset(self) -> None:
        """Drop the stream; a retry reuses a granted share when possible."""

        self.close()

    def close(self) -> None:
        """Tear the pipeline and the portal session down."""

        for pipeline in (self._pipeline, self._converter):
            if pipeline is not None and self._gst is not None:
                try:
                    pipeline.set_state(self._gst.State.NULL)
                except Exception:  # pragma: no cover - teardown must not fail
                    pass
        self._pipeline = None
        self._sink = None
        self._frame_size = None
        self._node = None
        self._converter = None
        self._converter_size = None
        self._forget_frames()
        self.close_session()

    def _forget_frames(self) -> None:
        with self._lock:
            self._latest = None
            self._fresh = False
            self._arrived.clear()
        self._converted = None
        self._converted_gray = None

    # -- portal ------------------------------------------------------------

    def _call(self, method: str, parameters, signature: str = "(o)"):
        """One ScreenCast portal method, waiting for its Response signal.

        The Response subscription is installed before the call: a valid
        restore token can produce the response immediately, and subscribing
        afterwards used to lose it and wait for the whole timeout. The
        request object path is deterministic (sender plus handle token), as
        the portal specification describes.
        """

        self._request_count += 1
        token = f"dasung{os.getpid()}_{self._request_count}"
        options_method = parameters
        if isinstance(parameters, tuple):
            options = dict(parameters[-1])
            options.setdefault("handle_token", self._glib.Variant("s", token))
            options_method = parameters[:-1] + (options,)
        sender = self._bus.get_unique_name().replace(":", "").replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        result: dict = {}
        loop = self._glib.MainLoop()
        state: dict = {"timeout": None}

        def on_response(_conn, _sender, signal_path, _iface, _signal, params):
            if signal_path != path:
                return
            result["code"], result["values"] = params.unpack()
            loop.quit()

        def on_timeout():
            state["timeout"] = None
            loop.quit()
            return False

        subscription = self._bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            self._gio.DBusSignalFlags.NONE,
            on_response,
        )
        state["timeout"] = self._glib.timeout_add(self._timeout_ms, on_timeout)
        try:
            reply = self._bus.call_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.ScreenCast",
                method,
                self._glib.Variant(signature, options_method),
                self._glib.VariantType.new("(o)"),
                self._gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            returned = reply.unpack()[0]
            if returned != path:
                raise CaptureError(
                    f"{method}: unexpected request path {returned}"
                )
            # The response may already have arrived while call_sync was
            # dispatching events: only run the loop when it has not.
            if "code" not in result:
                loop.run()
        finally:
            self._bus.signal_unsubscribe(subscription)
            if state["timeout"] is not None:
                self._glib.source_remove(state["timeout"])
        if "code" not in result:
            raise CaptureError(
                f"the screen share request timed out ({method})"
            )
        if result["code"] != 0:
            raise PortalRefused(
                f"the screen share request was refused ({method}, "
                f"code {result['code']})",
                int(result["code"]),
            )
        return result["values"]

    def _open_session(self, width: int) -> None:
        """Create the portal session and start the selected monitor stream."""

        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("Gst", "1.0")
        from gi.repository import Gio, GLib, Gst

        Gst.init(None)
        self._gio = Gio
        self._glib = GLib
        self._gst = Gst
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # A stable application id lets KDE store the ScreenCast permission
        # and its restore token for this program instead of the process.
        try:
            self._bus.call_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.host.portal.Registry",
                "Register",
                GLib.Variant("(sa{sv})", (APP_ID, {})),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception:  # pragma: no cover - the portal may not offer it
            pass

        tokens = []
        if self._restore_token:
            tokens.append(self._restore_token)
        # A stale token must not block the share dialog: retry without it.
        tokens.append(None)
        last_error: Exception | None = None
        started = False
        for token in tokens:
            try:
                self._start_session(token)
                started = True
                break
            except PortalRefused as exc:
                last_error = exc
                self.close_session()
                if exc.code == 1:
                    # The user dismissed the dialog: asking again straight
                    # away would open a second one for nothing.
                    break
                continue
            except CaptureError as exc:
                last_error = exc
                self.close_session()
                continue
        if not started:
            raise last_error or CaptureError("the screen share could not start")

        props = self._stream_properties
        size = props.get("size")
        if not size:
            raise CaptureError("the shared stream has no size")
        self._source = (int(size[0]), int(size[1]))
        position = props.get("position")
        if position:
            self._origin = (int(position[0]), int(position[1]))
        frame_width, frame_height = scale_dimensions(
            self._source[0], self._source[1], width
        )
        self._start_pipeline(self._node, frame_width, frame_height)

    def close_session(self) -> None:
        """Close only the portal session, keeping a possible token."""

        if self._session is not None and self._bus is not None:
            try:
                self._bus.call_sync(
                    "org.freedesktop.portal.Desktop",
                    self._session,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    self._gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
            except Exception:  # pragma: no cover - the session may be gone
                pass
        self._session = None

    def _start_session(self, restore_token: str | None) -> None:
        created = self._call(
            "CreateSession",
            (
                {"session_handle_token": self._glib.Variant("s", f"dasung{os.getpid()}")},
            ),
            "(a{sv})",
        )
        self._session = created["session_handle"]
        options = {
            "types": self._glib.Variant("u", 1),  # monitor
            "multiple": self._glib.Variant("b", False),
            "cursor_mode": self._glib.Variant("u", 1),  # hidden
            "persist_mode": self._glib.Variant("u", 2),
        }
        if restore_token:
            options["restore_token"] = self._glib.Variant("s", restore_token)
        self._call(
            "SelectSources",
            (self._session, options),
            "(oa{sv})",
        )
        started = self._call(
            "Start",
            (self._session, "", {}),
            "(osa{sv})",
        )
        streams = started.get("streams") or []
        if not streams:
            raise CaptureError("the screen share returned no stream")
        self._node = int(streams[0][0])
        self._stream_properties = streams[0][1]
        token = started.get("restore_token")
        if token and token != self._restore_token:
            self._restore_token = token
            try:
                save_restore_token(token)
            except OSError:
                pass

    def _stream_description(self, node: int) -> str:
        """The raw stream pipeline (system memory, any pixel format)."""

        return (
            f"pipewiresrc path={node} ! video/x-raw ! "
            "appsink name=ghostsink max-buffers=1 drop=true sync=false "
            "emit-signals=true"
        )

    def _start_pipeline(self, node: int, width: int, height: int) -> None:
        """Build the stream pipeline; the PipeWire node may need a retry.

        Right after the portal hands the node over, the first connection can
        fail with "target not found"; rebuilding once after a short pause is
        enough in practice.
        """

        description = self._stream_description(node)
        last_error = ""
        for attempt in range(2):
            pipeline = self._gst.parse_launch(description)
            if pipeline is None:
                raise CaptureError("cannot build the screen capture pipeline")
            sink = pipeline.get_by_name("ghostsink")
            sink.connect("new-sample", self._on_new_sample)
            state = pipeline.set_state(self._gst.State.PLAYING)
            if state == self._gst.StateChangeReturn.FAILURE:
                pipeline.set_state(self._gst.State.NULL)
                last_error = "the screen capture pipeline failed to start"
            else:
                result, _current, _pending = pipeline.get_state(
                    5 * self._gst.SECOND
                )
                if result == self._gst.StateChangeReturn.FAILURE:
                    pipeline.set_state(self._gst.State.NULL)
                    last_error = "the shared screen stream could not be opened"
                else:
                    self._pipeline = pipeline
                    self._sink = sink
                    self._frame_size = (width, height)
                    self._node = node
                    return
            if attempt == 0:
                time.sleep(0.3)
        raise CaptureError(last_error or "cannot start the screen capture pipeline")

    def _restart_pipeline(self) -> None:
        """Rebuild the stream with the already granted node and size."""

        if self._pipeline is not None:
            self._pipeline.set_state(self._gst.State.NULL)
            self._pipeline = None
            self._sink = None
        self._forget_frames()
        if self._node is not None and self._frame_size is not None:
            self._start_pipeline(self._node, *self._frame_size)

    # -- frames ------------------------------------------------------------

    def _on_new_sample(self, sink):
        """Stream thread: keep the newest frame, convert nothing."""

        sample = sink.emit("pull-sample")
        if sample is not None:
            with self._lock:
                self._latest = sample
                self._fresh = True
            self._arrived.set()
        return self._gst.FlowReturn.OK

    def poll_changes(self) -> bool | None:
        """True when the stream delivered a frame since the last grab.

        The compositor sends frames only when the shared output changes, so
        a frame is a change. None before the stream runs.
        """

        if self._pipeline is None:
            return None
        with self._lock:
            return self._fresh

    def _take_latest(self, timeout: float):
        """The newest frame, waiting up to `timeout` for the first one."""

        with self._lock:
            sample = self._latest
            self._fresh = False
        if sample is not None:
            return sample
        self._arrived.wait(timeout)
        with self._lock:
            self._fresh = False
            return self._latest

    def _converter_for(self, width: int, height: int):
        """The appsrc -> GRAY8 pipeline for this model size (built once).

        `bilinear2` averages every source pixel under a model pixel, like
        the X11 GdkPixbuf reduction; the default two-tap bilinear skipped
        thin strokes, so text flickered in and out of the model (1-pixel
        lines every 7 pixels came out as 0 or 255 instead of a steady grey).
        """

        if self._converter is not None and self._converter_size == (width, height):
            return self._converter
        if self._converter is not None:
            self._converter.set_state(self._gst.State.NULL)
        converter = self._gst.parse_launch(
            "appsrc name=rawsrc format=time ! videoconvert ! "
            "videoscale method=bilinear2 ! "
            f"video/x-raw,format=GRAY8,width={width},height={height} ! "
            "appsink name=graysink sync=false"
        )
        if converter is None:
            raise CaptureError("cannot build the frame conversion pipeline")
        if converter.set_state(self._gst.State.PLAYING) == (
            self._gst.StateChangeReturn.FAILURE
        ):
            converter.set_state(self._gst.State.NULL)
            raise CaptureError("the frame conversion pipeline failed to start")
        self._converter = converter
        self._converter_size = (width, height)
        return converter

    def _convert(self, sample, width: int, height: int) -> bytes:
        """One raw frame to GRAY8 bytes at the model size."""

        if sample is self._converted and self._converted_gray is not None:
            return self._converted_gray
        converter = self._converter_for(width, height)
        converter.get_by_name("rawsrc").emit("push-sample", sample)
        converted = converter.get_by_name("graysink").emit(
            "try-pull-sample", 2 * self._gst.SECOND
        )
        if converted is None:
            raise CaptureError("cannot convert the captured frame")
        buffer = converted.get_buffer()
        size = buffer.get_size()
        data = self._extract(buffer, size)
        row = width
        if size == row * height:
            gray = data
        elif height and size % height == 0 and size // height >= row:
            stride = size // height
            gray = b"".join(
                data[y * stride : y * stride + row] for y in range(height)
            )
        else:
            raise CaptureError("the captured frame has an odd size")
        self._converted = sample
        self._converted_gray = gray
        return gray

    def grab(self, width: int) -> Frame:
        """Open the portal session on first use, then convert the latest frame."""

        if self._pipeline is None:
            self._open_session(width)
        assert self._pipeline is not None and self._sink is not None
        assert self._frame_size is not None and self._source is not None
        sample = self._take_latest(1.0)
        if sample is None:
            self._restart_pipeline()
            if self._sink is not None:
                sample = self._take_latest(2.0)
        if sample is None:
            self.close()
            raise CaptureError("no frame from the shared screen")
        frame_width, frame_height = self._frame_size
        try:
            gray = self._convert(sample, frame_width, frame_height)
        except CaptureError:
            self.close()
            raise
        self.source_origin = self._origin
        return Frame(
            gray=gray,
            width=frame_width,
            height=frame_height,
            source_width=self._source[0],
            source_height=self._source[1],
        )

    @staticmethod
    def _extract(buffer, size: int) -> bytes:
        try:
            return bytes(buffer.extract_dup(0, size))
        except Exception:  # pragma: no cover - older bindings
            ok, info = buffer.map(0)
            if not ok:
                raise CaptureError("cannot read the captured frame")
            try:
                return bytes(info.data)[:size]
            finally:
                buffer.unmap(info)


def open_capture(
    output: str = "auto", restore_token: str | None = None, *, panel=None
):
    """Pick the backend for this session; capture stays lazy.

    Wayland (any compositor) uses the portal; X11 uses the GTK root-window
    grab. ``output`` names the monitor for the X11 backend and is ignored on
    Wayland, where the share dialog picks the screen. ``panel`` is the panel
    profile used to auto-detect the monitor by its EDID name on X11.
    """

    profile = get_panel(panel)
    if os.environ.get("WAYLAND_DISPLAY"):
        return WaylandCapture(restore_token=restore_token)
    import gi

    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf

    return X11Capture(
        Gdk, GdkPixbuf, wanted=output, panel_names=profile.edid_names
    )
