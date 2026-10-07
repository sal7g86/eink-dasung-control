"""Screen capture backends for the ghost watcher.

X11 grabs the root window in the Dasung monitor's region through GTK. KDE
Wayland goes through the ScreenCast portal and reads the PipeWire stream with
GStreamer: KWin's own ScreenShot2 API is allowlisted to installed screenshot
applications, so a script cannot use it. Both backends import ``gi`` lazily,
so this module stays importable in the project virtualenv where the GTK
bindings do not exist.

No image is ever written to disk: each backend returns grayscale bytes at
the model resolution, with the coordinates of the captured region so the
elements can be reported in screen pixels.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

from . import paths
from .ghostwatch import CaptureError, Frame
from .panels import get_panel


# The portal waits for the user to pick a monitor the first time.
SESSION_TIMEOUT_MS = 120_000
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


def x11_monitor_aliases(monitors) -> list[tuple[str, ...]]:
    """EDID names for the Gdk monitors, matched by RandR geometry.

    The returned list is parallel to `monitors`; an empty tuple means no
    EDID match (geometry scaled, output unplugged, xlib missing).
    """

    edid_names = x11_edid_monitor_names()
    if not edid_names:
        return []
    aliases: list[tuple[str, ...]] = []
    for monitor in monitors:
        geometry = monitor.get_geometry()
        name = edid_names.get(
            (geometry.x, geometry.y, geometry.width, geometry.height)
        )
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
    """Grab the Dasung region of the X11 root window with GTK."""

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
        # Global origin of the captured region (None until the first grab);
        # the zone provider needs it to convert window rectangles.
        self.source_origin: tuple[int, int] | None = None

    def reset(self) -> None:
        """Resolve the monitor again on the next grab."""

        self._monitor = None

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
        """Grab the resolved monitor, reduced to `width` model columns."""

        monitor = self._resolve()
        geometry = monitor.get_geometry()
        if geometry.width <= 0 or geometry.height <= 0:
            raise CaptureError("the monitor has no usable geometry")
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
        target_width, target_height = scale_dimensions(
            geometry.width, geometry.height, width
        )
        small = pixbuf.scale_simple(
            target_width, target_height, self._pixbuf.InterpType.BILINEAR
        )
        if small is None:
            raise CaptureError("cannot scale the captured frame")
        gray = gray_from_channels(
            small.get_pixels(),
            small.get_width(),
            small.get_height(),
            small.get_n_channels(),
            small.get_rowstride(),
        )
        self.source_origin = (geometry.x, geometry.y)
        return Frame(
            gray=gray,
            width=target_width,
            height=target_height,
            source_width=geometry.width,
            source_height=geometry.height,
        )


class WaylandCapture:
    """ScreenCast portal session plus a GStreamer GRAY8 pipeline.

    The portal dialog appears the first time and asks which screen to share;
    with persist mode the returned restore token makes later sessions silent.
    The stream stays open between samples and only the latest frame is kept.
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

        if self._pipeline is not None and self._gst is not None:
            try:
                self._pipeline.set_state(self._gst.State.NULL)
            except Exception:  # pragma: no cover - teardown must not fail
                pass
        self._pipeline = None
        self._sink = None
        self._frame_size = None
        self._node = None
        self.close_session()

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

    def _start_pipeline(self, node: int, width: int, height: int) -> None:
        """Build the GStreamer pipeline; the PipeWire node may need a retry.

        Right after the portal hands the node over, the first connection can
        fail with "target not found"; rebuilding once after a short pause is
        enough in practice.
        """

        description = (
            f"pipewiresrc path={node} ! videoconvert ! videoscale ! "
            f"video/x-raw,format=GRAY8,width={width},height={height} ! "
            "appsink name=ghostsink max-buffers=1 drop=True sync=false"
        )
        last_error = ""
        for attempt in range(2):
            pipeline = self._gst.parse_launch(description)
            if pipeline is None:
                raise CaptureError("cannot build the screen capture pipeline")
            sink = pipeline.get_by_name("ghostsink")
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
        if self._node is not None and self._frame_size is not None:
            self._start_pipeline(self._node, *self._frame_size)

    # -- frames ------------------------------------------------------------

    def _pull(self, timeout_ns: int):
        """One appsink action signal: the PyGObject bindings expose

        ``try-pull-sample`` as an action signal, not as a method.
        """

        return self._sink.emit("try-pull-sample", timeout_ns)

    def grab(self, width: int) -> Frame:
        """Open the portal session on first use, then pull one frame."""

        if self._pipeline is None:
            self._open_session(width)
        assert self._pipeline is not None and self._sink is not None
        assert self._frame_size is not None and self._source is not None
        sample = self._pull(self._gst.SECOND)
        if sample is None:
            self._restart_pipeline()
            if self._sink is not None:
                sample = self._pull(2 * self._gst.SECOND)
        if sample is None:
            self.close()
            raise CaptureError("no frame from the shared screen")
        buffer = sample.get_buffer()
        size = buffer.get_size()
        data = self._extract(buffer, size)
        frame_width, frame_height = self._frame_size
        row = frame_width
        if size == row * frame_height:
            gray = data
        elif frame_height and size % frame_height == 0:
            stride = size // frame_height
            if stride < row:
                self.close()
                raise CaptureError("the captured frame has an odd layout")
            gray = b"".join(
                data[y * stride : y * stride + row] for y in range(frame_height)
            )
        else:
            self.close()
            raise CaptureError("the captured frame has an odd size")
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
