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
sudo apt install python3-serial python3-gi gir1.2-gtk-3.0 \
  gir1.2-ayatanaappindicator3-0.1 python3-xlib
git clone https://github.com/sal7g86/eink-dasung-control.git
cd eink-dasung-control
PYTHONPATH=src python3 -m dasungctl
```

`python3` must be the interpreter that owns the GTK bindings (on Mint and
Ubuntu it is `/usr/bin/python3`). To get a `dasungctl` command on `PATH`,
either `pipx install .` or `python3 -m pip install --user .` (the latter
needs `--break-system-packages` on distributions with PEP 668), or write a
small launcher that points at the checkout:

```console
mkdir -p ~/.local/bin
cat > ~/.local/bin/dasungctl <<'EOF'
#!/bin/sh
exec env PYTHONPATH="/path/to/eink-dasung-control/src" python3 -m dasungctl "$@"
EOF
chmod +x ~/.local/bin/dasungctl
```

All three ways work: `pipx` keeps the program in its own virtualenv without
GTK bindings, and the tray re-executes itself with the system Python that
has them (see [tray.md](tray.md)).

`dasungctl tray --install-autostart` prefers such a launcher over its direct
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
output seen in `xrandr`.

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

`python3-pyserial` is required there because the tray runs with the system
Python, where the GTK bindings live: the re-exec probe looks for `gi`,
`AyatanaAppIndicator3` and `serial` in the same interpreter (see
[tray.md](tray.md)).

When the checkout lives on a removable disk, keep the environment inside it
(for example `.venv-fedora`, so that sources and Python dependencies stay
together — the tray re-executes itself with the system Python for the GTK
bindings) and expose the command through a launcher on `PATH` rather than a
direct symlink: a launcher can report a missing disk, while a symlink to an
unmounted disk simply stops working. `dasungctl tray --install-autostart`
prefers such a launcher (`~/.local/bin/dasungctl`, when it is executable)
over its direct command line, so the login entry shows the same warning
instead of failing silently.

```console
uv venv --python /usr/bin/python3 .venv-fedora
uv pip install --python .venv-fedora/bin/python -e ".[dev]"
```

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

Tests and lint do not need GTK or the monitor; a virtualenv keeps their
dependencies separate:

```console
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The tray can also be started from this environment: it re-executes itself
with the system Python, where the GTK bindings live.

See [development.md](development.md) for the test, lint and tooling details.
