# Panel profiles

`dasungctl` speaks the Dasung serial protocol family, but every monitor model
has its own confirmed values: display-mode numbers, speed labels, frontlight
presets and temperature mapping, the selector fields worth reading and their
limits, and the EDID name used to find the monitor for the ghost capture.
All of it lives in one `PanelProfile` (`src/dasungctl/panels.py`), so
supporting another model means filling a profile — not touching the
controller, the state, the UI or the capture code.

Only the profile below is confirmed and shipped in 0.1. Other models need the
same evidence captured first; until then they are untested.

## Shipped profile

`paperlike-hd-13.3` — **Dasung Paperlike HD Revolutionary 13.3″**, the
HD-FT variant with frontlight and touchscreen, 40 Hz refresh, protocol
`0x30`, EDID model `Paperlike H D`:

| Field | Value |
| --- | --- |
| `modes` | `1` auto, `2` text, `3` graphic, `4` video |
| `speed_labels` | `Fast`, `Fast+`, `Fast++`, `Fast+++`, `Fast++++` (values 1..5) |
| `frontlight_levels` | `0` Off, then ten levels of 10 (`10`..`100`) |
| `frontlight_modes` | `0` off, `1` cold, `2` warm, `3` mixed |
| `preset_temperatures` | cold `100`, warm `0`, mixed `70` |
| `custom_frontlight_mode` | `4` (project-invented manual temperature) |
| `temperature_levels` | `100, 89, 78, 67, 56, 44, 33, 22, 11, 0` (levels 1..10) |
| `read_fields` | mode, contrast, speed, frontlight, temperature, frontlight mode |
| `limits` | contrast `1..9`, speed `1..5`, frontlight/temperature `0..255`, frontlight mode `0..4` |

The evidence for every value is in [protocol.md](protocol.md); the static
analysis behind the labels is in
[research-findings.md](research-findings.md).

## What a profile describes

| Field | Meaning | Where the evidence comes from |
| --- | --- | --- |
| `key` | name used by the `panel` config key and by `doctor` | — |
| `name`, `protocol`, `refresh_hz` | model identity and the protocol version to expect | `dasungctl doctor`, EDID |
| `edid_names` | substrings of the monitor name used by the X11 capture auto-detect | `xrandr --props`, `doctor` |
| `modes` | display-mode value → name | physical `M` button calibration |
| `speed_labels` | speed value 1..N → label | official-client combo, confirmed on the panel when possible |
| `frontlight_levels` | brightness byte → label | physical lamp-button calibration |
| `frontlight_modes` | frontlight-mode value → name | physical lamp-button calibration |
| `preset_temperatures` | temperature the preset sends before its mode | serial capture |
| `custom_frontlight_mode` | project value for a manual temperature | project hypothesis, not firmware |
| `temperature_levels` | raw bytes of the manual 1..N levels | endpoint calibration + interpolation |
| `read_fields` | selectors the tray reads and persists | confirmed reads |
| `limits` | accepted range per read field | confirmed reads |

## Adding another model

1. **Identify it.** `dasungctl doctor` prints the protocol version and every
   selector it can read; the EDID model name and the current capture are in
   [protocol.md](protocol.md).
2. **Capture the same evidence.** Mode mapping with the physical `M` button,
   frontlight modes and brightness levels with the lamp button, speed steps
   with `M` + `-`/`+`, and the accepted range of every field worth exposing.
   Record the exact frames and the calibration in
   [protocol.md](protocol.md), as the current model's section does.
3. **Fill a `PanelProfile`** in `src/dasungctl/panels.py` and add it to
   `PANELS`; select it with the `panel` key in `config.json`. Nothing else
   in the code needs changing.
4. **Add tests** for the new tables, following `tests/test_panels.py`.
5. **Document** the new model here and in the changelog.

Rules:

- Only **confirmed** values belong in a profile; everything else stays an
  open question in [protocol.md](protocol.md).
- **Never copy calibrations between models.** The same wire value can mean
  something different on another panel.
- `doctor` already warns when the protocol version read from the monitor
  does not match the selected profile ("panel protocol" check).
- A profile that has not been tested on hardware must say so in its name or
  comment. 0.1 ships only the confirmed `paperlike-hd-13.3`.
