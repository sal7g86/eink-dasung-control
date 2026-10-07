# Installation

`dasungctl` requires Python 3.10 or newer and a Dasung Paperlike monitor
connected through its CH340 serial interface. The tray needs a Linux desktop
with GTK 3 and, on GNOME, the AppIndicator shell extension; the serial
diagnostics (`devices`, `doctor`) work without a graphical session.

## Quick install (no virtualenv)

The tray runs with the system Python, where the GTK bindings live, so no
environment is needed: install the distribution packages and run the
program from the checkout.

```console
# Debian/Ubuntu (Mint, X11)
sudo apt install python3-serial python3-gi gir1.2-gtk-3.0 \
  gir1.2-ayatanaappindicator3-0.1 python3-xlib
# Fedora (KDE, Wayland)
sudo dnf install python3-pyserial python3-gobject gtk3 \
  libayatana-appindicator-gtk3 gtk-layer-shell
git clone https://github.com/sal7g86/eink-dasung-control.git
cd eink-dasung-control
python3 src/_source_run.py
```

The last package in each line is session-specific: on X11 `python-xlib`
enables the window labels (and the panel auto-detect), on Wayland
`gtk-layer-shell` enables local zone clearing (KDE and wlroots compositors;
GNOME does not expose layer-shell to applications, so there the rest works
but clearing reports itself unavailable).

On X11 the estimate reads the Dasung region of the root window and labels its
areas through EWMH. On Wayland the capture goes through the ScreenCast
portal: the first sample shows a share dialog — pick the Dasung monitor
there, and the capture stays on that monitor only — and the grant is
remembered, so later starts open the session silently. On KDE Wayland the
window labels come from KWin scripting and need no extra package.

`python3` must be the interpreter that owns the GTK bindings (on Mint,
Ubuntu and Fedora it is `/usr/bin/python3`). To get a `dasungctl` command
on `PATH`, the tray can write a small launcher that runs this checkout with
the system Python:

```console
dasungctl tray --install-launcher   # ~/.local/bin/dasungctl
```

The launcher needs nothing installed and reports a checkout that cannot be
reached (for example a removable disk that is not mounted) instead of
failing silently. `pipx install .` and `python3 -m pip install --user .`
(the latter needs `--break-system-packages` on distributions with PEP 668)
also work: pipx keeps the program in its own virtualenv without GTK
bindings, and the tray re-executes itself with the system Python that has
them (see [tray.md](tray.md)); the launcher is the simplest route when the
checkout is already there.

`dasungctl tray --install-autostart` prefers the launcher over its direct
command line, so the login entry keeps working from the checkout.

## Optional X11 window labels

On X11 the ghost estimate labels its areas with the application class, which
needs `python-xlib`; the tray runs with the system Python, so install the
distribution package (`sudo apt install python3-xlib` on Mint/Ubuntu,
`sudo dnf install python3-xlib` on Fedora) or the `labels` extra when working
from the project virtualenv.

The same library lets the estimate auto-detect the Dasung among several
monitors, because Gdk only reports the RandR output name (`DP-1`) while the
EDID carries the model. Without it the estimate still works, just without
application names, and on multi-monitor X11 `ghost.output` must name the
output seen in `xrandr`. On KDE Wayland the labels come from KWin scripting
and need no extra package; the panel-presence check reads `/sys/class/drm`
there.

## GTK 3 and Ayatana AppIndicator

The optional tray icon needs the system GTK 3 and Ayatana AppIndicator
bindings (no pip packages):

```console
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
# GNOME additionally needs the AppIndicator shell extension, e.g. on Ubuntu:
# sudo apt install gnome-shell-extension-appindicator
```

On Fedora the same packages are:

```console
sudo dnf install python3-gobject gtk3 libayatana-appindicator-gtk3 python3-pyserial
# GNOME additionally needs: sudo dnf install gnome-shell-extension-appindicator
```

On Wayland the tray additionally needs `gtk-layer-shell` for local zone
clearing (`sudo dnf install gtk-layer-shell` on Fedora); without it, or on
GNOME (which does not expose layer-shell to applications), the rest works
but clearing reports itself unavailable. The screen capture uses the
ScreenCast portal, which ships with the desktop on Fedora and KDE.

`python3-pyserial` is required there because the tray runs with the system
Python, where the GTK bindings live: the re-exec probe looks for `gi`,
`AyatanaAppIndicator3` and `serial` in the same interpreter (see
[tray.md](tray.md)).

On KDE Wayland the GTK theme comes from the desktop Settings portal: the KDE
backend answers only for `org.freedesktop.appearance`, so the theme name
falls back to the GNOME `org.gnome.desktop.interface gtk-theme`. A leftover
GNOME theme there (a dark theme installed once and never reset) makes these
and every other GTK window dark while KDE uses a light color scheme —
`~/.config/gtk-3.0/settings.ini` is not enough, the portal value wins. Align
it with the desktop:

```console
gsettings set org.gnome.desktop.interface gtk-theme Breeze
```

`Adwaita` (the GNOME default) or any installed light theme works too; on KDE
the Breeze GTK theme matches the desktop.

When the checkout lives on a removable disk, expose the command through the
generated launcher on `PATH` rather than a direct symlink: a launcher can
report a missing disk (with a desktop notification), while a symlink to an
unmounted disk simply stops working. `dasungctl tray --install-autostart`
prefers the launcher (`~/.local/bin/dasungctl`, when it is executable) over
its direct command line, so the login entry shows the same warning instead
of failing silently.

```console
dasungctl tray --install-launcher
dasungctl tray --install-autostart
```

The launcher runs the checkout with the system Python; a development
virtualenv inside the disk is only needed for tests and lint (see
[development.md](development.md)) and is not part of the command.

## Connecting the monitor

`dasungctl` auto-detects the monitor's CH340 by its confirmed USB ID
`1a86:7523`. Its device name is often `/dev/ttyUSB0`, but the number can
change. List what it can currently see with:

```console
dasungctl devices
```

If no device is listed, make sure the monitor is powered on and reconnect its
USB data cable. Then inspect USB enumeration and the kernel log:

```console
lsusb | grep -i '1a86:7523'
journalctl -k -b --grep='ttyUSB\|ch34' --no-pager
```

When the device is present, check its ownership:

```console
ls -l /dev/ttyUSB0
groups
```

On distributions that assign serial devices to `dialout` (Debian, Ubuntu,
Fedora), add your account if needed, then log out and back in; a single shell
can pick up the new group right away with `newgrp dialout`:

```console
sudo usermod -aG dialout "$USER"
```

Some Linux installations run `brltty`, which can claim QinHeng CH340 devices.
If `/dev/ttyUSB0` repeatedly disappears, inspect `brltty` and the kernel log
before changing system configuration:

```console
systemctl status brltty
journalctl -u brltty
journalctl -k --grep='ttyUSB\|ch34'
```

## Development environment

Tests and lint do not need GTK or the monitor; a virtualenv inside the
checkout keeps their dependencies separate. Each machine keeps its own
environment, so a checkout shared between two systems holds both:

```console
# Linux Mint machine (X11): .venv
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

```console
# Fedora machine (Wayland): .venv-fedora, built on the system Python
uv venv --python /usr/bin/python3 .venv-fedora
uv pip install --python .venv-fedora/bin/python -e ".[dev]"
.venv-fedora/bin/python -m pytest -q
```

After moving source files, refresh the editable install of the machine you
are on (`uv pip install --python <venv>/bin/python -e ".[dev]"`). The tray
can also be started from either environment: it re-executes itself with the
system Python, where the GTK bindings live.

See [development.md](development.md) for the test, lint and tooling details.
