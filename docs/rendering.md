# Rendering on e-ink panels

An e-ink panel shows shades of gray only: it has no RGB subpixels, it
refreshes slowly, and its pixel density is high. Desktop defaults written
for LCD screens — subpixel antialiasing, fractional scaling that enlarges
the desktop — look dirty or blurry there. This page collects the settings
that keep text crisp on a Dasung panel, with the commands to verify them.
All of it is desktop configuration; it works without `dasungctl`, and the
tray follows it (see the note at the end).

## Antialiasing: force grayscale

Subpixel antialiasing ("LCD filtering") computes colored fringes for the
RGB stripes of an LCD. A grayscale panel has no stripes to place them on,
so they land as irregular gray along the glyph edges. Force grayscale
everywhere:

- Cinnamon: Settings → Fonts → Antialiasing **Grayscale** (Hinting
  `Slight` is a good starting point). The value is exported through
  XSETTINGS and covers GTK and Qt applications.
- Applications that read fontconfig directly — Chromium, Electron, some
  Qt builds — ignore XSETTINGS. Add a user override in
  `~/.config/fontconfig/fonts.conf`:

  ```xml
  <?xml version="1.0"?>
  <!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
  <fontconfig>
    <match target="font">
      <edit name="rgba" mode="assign"><const>none</const></edit>
    </match>
  </fontconfig>
  ```

  and check it with `fc-match -v sans-serif | grep rgba` (`5` = none).
  Applications read the override at startup, so restart them to see it.
- KDE's Font module already writes `rgba=none`; Wayland compositors rarely
  use subpixel at all.

## Scaling: never enlarge the desktop

A compositor that draws the desktop at its logical size and then magnifies
it to the panel turns every glyph into a magnified small bitmap. Cinnamon's
X11 fractional scaling does exactly that in its default `scale-up` mode:

```console
$ gsettings get org.cinnamon.muffin.x11 fractional-scale-mode
'scale-up'
```

`scale-ui-down` renders the toolkits at the next integer scale (2×) and
reduces the picture to the requested factor, the same approach KDE uses on
Wayland:

```console
$ gsettings set org.cinnamon.muffin.x11 fractional-scale-mode 'scale-ui-down'
```

The change needs Cinnamon's `scale-monitor-framebuffer` and
`x11-randr-fractional-scaling` experimental features (the Display settings
enable them when fractional scaling is turned on) and a logout/login to
take effect. A fractional factor still resamples once (2 / 1.75 ≈ 1.14);
an **integer** monitor scale (200%) maps pixels 1:1 and is the sharpest
option, at the price of a 14% larger interface on that monitor.

Check what the compositor is doing with:

```console
$ xrandr --verbose | grep -A14 '^DP-1 ' | grep -E 'Transform:|filter'
	Transform:  1.141815 0.000000 0.000000
	           filter: bilinear
```

A transform **below 1 enlarges** the desktop (blurry), **above 1 reduces**
it (crisp), exactly 1 is 1:1. `DP-1` is the output name seen in `xrandr`.

## Hinting

Hinting snaps stems to the pixel grid. `slight` (the common default) is
softer; `medium` and `full` give crisper stems on e-ink at the price of
slightly stiffer letterforms — try them on the panel and keep the one you
prefer:

```console
$ gsettings set org.cinnamon.settings-daemon.plugins.xsettings hinting 'full'
$ gsettings get org.cinnamon.settings-daemon.plugins.xsettings hinting
'full'
```

For applications that read fontconfig directly, add the matching rule to
`fonts.conf`:

```xml
  <match target="font">
    <edit name="hintstyle" mode="assign"><const>hintfull</const></edit>
  </match>
```

Verify with `xrdb -query | grep -i xft` and
`fc-match -v sans-serif | grep hintstyle` (`1` slight, `2` medium, `3`
full).

## Text size and DPI

Text size follows `Xft.dpi`, which the desktop derives from the monitor
scale and `text-scaling-factor`:

```console
$ gsettings set org.cinnamon.desktop.interface text-scaling-factor 1.1
```

What reaches the eye is the rendered DPI divided by the compositor
transform: at integer scale 200% with factor 1.1, `Xft.dpi` is 211 and
text appears at 211 dpi; the same factor under `scale-ui-down` at 1.75
shows it at 211 / 1.14 ≈ 185 dpi. The factor is global — it changes the
text on every monitor, scaled or not — and it applies live, so tune it
while looking at the panel. Somewhere around 160–200 dpi reads well on a
13.3-inch panel.

## Verify the chain

```console
$ xrdb -query | grep -i xft
Xft.dpi:	211
Xft.rgba:	none
Xft.hintstyle:	hintfull
$ fc-match -v sans-serif | grep -E 'rgba:|hintstyle:'
	rgba: 5(i)(w)
	hintstyle: 3(i)(w)
$ xrandr --verbose | grep -A14 '^DP-1 ' | grep -E 'Transform:|filter'
	Transform:  1.000000 0.000000 0.000000
```

## dasungctl note

Since 0.1.5 the tray's X11 monitor matching, window labels and ghost
capture follow the session's window scale, so a scaled desktop no longer
hides the panel or breaks the estimate. After changing the display
settings, restart the tray: the capture and the label provider cache the
monitor geometry at startup.
