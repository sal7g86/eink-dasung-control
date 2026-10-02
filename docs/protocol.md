# Dasung Paperlike HD serial protocol notes

This document records observations for a Dasung Paperlike HD Revolutionary
13.3-inch monochrome 40 Hz monitor, the HD-FT variant with frontlight and
touchscreen. Confirmed observations are kept separate from information
inherited from other models and from hypotheses.

## Confirmed hardware observations

The monitor exposes an internal USB hub with these devices:

| USB ID | Device | Observed role |
| --- | --- | --- |
| `1a86:7523` | QinHeng CH340 serial converter | Linux exposes `/dev/ttyUSB0`; working control channel |
| `1a86:5512` | QinHeng CH341 EPP/MEM/I2C adapter | Not used by this project |
| `27c0:0818` | Paperlike HD-FT | Two HID interfaces: touchscreen and vendor-specific 63-byte IN/OUT reports |

On the development system, `brltty` initially claimed the CH340. Masking its
regular and udev services made `/dev/ttyUSB0` stable.

The Dasung EDID appears on `/dev/i2c-12` as model `Paperlike H D`, manufactured
in week 40 of 2024. Standard DDC/CI communication fails. Sending the old
paperlike-go clear packet `03 08 00 00 06 03` to I2C address `0x37` returned an
I/O error. This project therefore does not use that older DDC/I2C protocol.

## Confirmed serial transport

The working channel is the CH340 at:

- 115200 baud
- 8 data bits
- 1 stop bit
- no parity
- no hardware or software flow control

Commands and responses are 24 ASCII hexadecimal characters representing a
12-byte packet. They are not sent as 12 raw binary bytes. Confirmed requests
use uppercase hexadecimal, without spaces or a line terminator.

All observed packets begin with `5F F5` and end with `A0 FA`.

The general shape of a request is recorded positionally as:

```text
5FF5 CC VV [6-byte payload] A0FA
```

`CC` and `VV` are convenient names for byte offsets 2 and 3, not universal
semantic claims. Their meanings depend on the command family. Confirmed read
requests leave all six payload bytes at zero. Official-client static analysis
has since found a command-specific non-zero use for the six bytes; it is
documented separately below and has not been tested on this monitor.

### Confirmed read request layout

| Byte offset | Length | Observed value or meaning |
| ---: | ---: | --- |
| 0 | 2 | Prefix `5F F5` |
| 2 (`CC`) | 1 | Read marker `0A` |
| 3 (`VV`) | 1 | Parameter ID |
| 4 | 6 | Unknown request payload; zero in every tested read |
| 10 | 2 | Tail `A0 FA` |

### Confirmed read response layout

| Byte offset | Length | Observed value or meaning |
| ---: | ---: | --- |
| 0 | 2 | Prefix `5F F5` |
| 2 | 1 | `F0` in every recorded response; meaning unknown |
| 3 | 1 | `0A`, matching the read marker |
| 4 | 1 | Parameter ID |
| 5 | 5 | Response data |
| 10 | 2 | Tail `A0 FA` |

## Confirmed read exchanges

| Parameter | ID | TX | RX | Observed result |
| --- | ---: | --- | --- | --- |
| Version | `10` | `5FF50A10000000000000A0FA` | `5FF5F00A103010000000A0FA` | Protocol version byte `30`; additional byte `10` |
| Contrast | `01` | `5FF50A01000000000000A0FA` | `5FF5F00A010600000000A0FA` | Raw value 6 |
| Mode | `02` | `5FF50A02000000000000A0FA` | `5FF5F00A020400000000A0FA` | Raw value 4 |
| Frontlight temperature | `08` | `5FF50A08000000000000A0FA` | `5FF5F00A080000000000A0FA` | Raw value 0 |
| Frontlight level | `09` | `5FF50A09000000000000A0FA` | `5FF5F00A092800000000A0FA` | Raw value `28` hexadecimal, or 40 decimal |

Additional selectors were read on 2026-09-18 while verifying the
official-client commands. The read layout is identical for each:

| Parameter | ID | TX | RX | Observed result |
| --- | ---: | --- | --- | --- |
| Selector `03` | `03` | `5FF50A03000000000000A0FA` | `5FF5F00A030100000000A0FA` | Raw value 1; meaning not established |
| Speed (official-client name) | `04` | `5FF50A04000000000000A0FA` | `5FF5F00A040400000000A0FA` | Raw value 4; official-client range `1..5` |
| Frontlight mode (official-client name) | `07` | `5FF50A07000000000000A0FA` | `5FF5F00A070200000000A0FA` | Raw value 2; value semantics unknown |
| Mux (official-client name) | `0B` | `5FF50A0B000000000000A0FA` | `5FF5F00A0B0100000000A0FA` | Raw value 1; semantics unconfirmed |
| Selector `11` | `11` | `5FF50A11000000000000A0FA` | `5FF5F00A110200000000A0FA` | Raw value 2; meaning unknown |

Selectors `12` and `13` produced no response in the same tests; see the
text-enhancement note under official-client static evidence.

`selector_03` was observed returning `1` and later `0` during the same
session, so it reports a live value rather than a stored setting. Its meaning
remains unknown and must not be inferred from the refresh command family alone.

The official macOS client labels the frontlight-mode options off, cold/white,
warm, and mixed (its strings are Chinese), and the speed options
`Fast` through `Fast++++`. Physical calibration later mapped the frontlight
labels to values `0/1/2/3` and confirmed that holding `M` while pressing `-`/`+`
changes speed; see the physical-control sections below. The Windows 25-inch
client presents mux as monitor slots `P0`-`P4`. On this target mux read `1` for
most of the session and `3` later, with no identified action causing the change;
no physical control altered it during calibration. Its meaning remains open.
The 13.3-inch HD has Mini-HDMI and USB Type-C video inputs, so a per-input state
is a plausible hypothesis, but the multi-monitor slot reading from the 25-inch
client is equally unsupported.

The device responds to the same protocol family used by paperlike-rs for the
Paperlike 13K Color 37 Hz. That project expects protocol version `0x31`; this
monitor returned `0x30`.

### Confirmed write acknowledgements

On 2026-09-18, no-op writes whose values had just been read back were sent:
contrast `04`, speed `04`, frontlight mode `02`, and mux `01`. Each answered
immediately with a 12-byte frame of this form:

```text
5FF5F0CC000000000000A0FA
```

`CC` matches the request command. This acknowledgement is distinct from the
`F0 0A` parameter response. Commands `12` and `20` produced no
acknowledgement. The earlier soft-refresh test used a send-only path and did
not read the serial input, so its acknowledgement behavior was not observed in
that test.

Later on 2026-09-18 the soft-refresh command `03` and the clock command `05`
were also confirmed to answer with `F0 CC` (`5FF5F003...` and `5FF5F005...`).
A full write-acknowledgement set observed so far is therefore `01`, `03`,
`04`, `05`, `07`, and `0B`; `12` and `20` never answered.

The monitor's acknowledgement is not perfectly serialized. During the same
session it occasionally emitted an unrelated stale request frame before or
instead of an acknowledgement, and at least once sent no frame at all. The
client therefore skips up to eight frames looking for the matching `F0 CC` and
only re-sends the idempotent write when the exchange itself times out. The generous
skip budget matters for rapid changes: a 2026-09-23 simulation of ten quick
temperature changes with three unsolicited frames per write made the old
three-frame budget re-send the command 28 times and fail, while the current
budget sent the expected 20 writes with no re-sends. This behaviour is a
monitor quirk, not a documented protocol feature, and must not be interpreted
as a second command channel.

### Unsolicited state frames after writes

A raw capture on 2026-09-23, after the frontlight-mode write
`5FF50704000000000000A0FA`, showed the acknowledgement followed by two
unsolicited frames in the ordinary write layout:

```text
5FF5F007000000000000A0FA   acknowledgement of command 07
5FF5091E000000000000A0FA   parameter 09 (frontlight level) = 30
5FF50805000000000000A0FA   parameter 08 (temperature) = 5
```

These are not acknowledgements and are not requested by the host. They carry
state the firmware reports while processing the command, and the frontlight
level frame is evidence that a mode write can change state that the host did
not set. The exact set, timing, and trigger of these frames is not mapped.

They pile up in front of the next response. Earlier client versions read one
frame and failed on them, and the leftover bytes then caused one-second serial
timeouts on later writes. The client now skips frames that do not match the
expected response, up to a bounded number, before failing. A read request is
sent only once per read, so skipped frames are not answered with extra
requests.

A separate 2026-09-23 measurement found that a read issued immediately after a
write pair can receive no response at all within the one-second timeout, while
the same read succeeds when issued later. The control tools therefore do not
verify a write with a read-back and rely on the acknowledgement; `Reload from
monitor` re-reads the monitor on request.

### Confirmed Revolutionary mode mapping

Each physical mode was selected on the monitor and then read with the
confirmed parameter-`02` request. This establishes the target model's complete
user-facing mapping without sending a mode change from software:

| Physical label | Raw value | Setter frame |
| --- | ---: | --- |
| Auto | `01` | `5FF50201000000000000A0FA` |
| Text | `02` | `5FF50202000000000000A0FA` |
| Graphic | `03` | `5FF50203000000000000A0FA` |
| Video | `04` | `5FF50204000000000000A0FA` |

The official macOS client accepts values `2..7`, but deeper analysis shows
that it supports several hardware profiles with different four-value tables.
That range must not be applied to the Revolutionary: this monitor confirms
`1..4`, including Auto value `1`.

The official-client profile query `5FF50A13000000000000A0FA` was sent once
with explicit approval. No response arrived during the one-second timeout, so
selector `13` remains unsupported or inconclusive on this firmware. On
2026-09-18 the macOS client's device-model form `5FF51300000000000000A0FA`
(command `13`, option `00`) was also tried and likewise produced no response.

### Confirmed physical-control calibration

On 2026-09-18 each physical control was pressed individually and the state was
read back with the confirmed read requests. The unit has six buttons, ordered
bottom to top on the bezel: `C`, `M`, `-`, `+`, lamp, and power.

| Control | Physical action | Observed read-back effect |
| --- | --- | --- |
| `C` | short or long press | visible full-screen refresh; no selector changed |
| `M` | short press | cycles display mode `auto -> text -> graphic -> video -> auto`; also re-applies the target mode's stored contrast (see below) |
| `-` / `+` | short or long press | contrast down/up by one step; long press behaved identically |
| hold `M`, press `-` / `+` | chord | speed down/up by one step (`1 <-> 2` and `4 <-> 5` observed); the monitor announces the new value with `5FF504VV` twice |
| lamp | short press | cycles frontlight mode `3 -> 1 -> 2 -> 0 -> 3`; also re-applies a level/temperature bundle (see below) |
| hold lamp, press `-` / `+` | chord | brightness down/up through `0` (off) and `10..100`; wraps around at both ends; announced with `5FF509VV` twice |
| lamp | long press | also selected the next frontlight mode |
| power | press | power on/off; all stored settings survived the cycle |

The 2026-09-18 tests found no brightness control: the lamp button alone never
changed the `frontlight` level (it stayed at `25` while on and `0` when off),
`-`/`+` change contrast, and holding `M` changes speed. On 2026-09-28 the
lamp + `-`/`+` chord was found to cycle the brightness through `0` (off) and
`10..100`; see "Confirmed frontlight level calibration".

### Confirmed frontlight mode mapping

The physical lamp button cycles four states. The read-back values, the official
macOS combo labels, and the observed light agree:

| `frontlight_mode` | macOS label | Observed light | `temperature` observed |
| ---: | --- | --- | ---: |
| `0` | off | off, `frontlight` 0 | 70 |
| `1` | cold | cold | 100 |
| `2` | warm | warm | 0 |
| `3` | mixed | medium | 0 and 70 |

The cycle order is `3 -> 1 -> 2 -> 0 -> 3`, so the macOS label order
off/cold/warm/mixed maps to values `0/1/2/3`.

The `temperature` field is a cold-to-warm balance: `0` is the warm end and
`100` the cold end. A sweep on 2026-09-28 wrote `0, 11, 22, ..., 100` and read
every value back unchanged, while `113`, `150`, `200`, and `255` all read back
as `100`: the firmware accepts `0..100` and clamps higher bytes. The cold
preset reads `100`, so it is the cold extreme; the mixed preset was observed
at both `0` and `70` in the earlier tests, and the tray sends `70`.

The project's `custom` frontlight mode (value `4`) is not a firmware mode:
writing `4` reads back as `3` (mixed) every time, while the temperature is
applied. The manual temperature levels therefore live in mixed mode too. The
tray maps the mixed read-back back to `custom` while the read temperature
equals the last manually set one, so the menu and the save/restore round trip
stay coherent; a mixed preset (or a lamp press) keeps temperature `70` and
still reads as `mixed`.

The tray exposes ten project levels over the `0..100` scale since 2026-09-28:
level `1` is the coldest endpoint (`100`), level `10` the warmest (`0`), and
the eight bytes between them are an even interpolation (`89`, `78`, `67`,
`56`, `44`, `33`, `22`, `11`).

### Confirmed frontlight level calibration

On 2026-09-28 the lamp + `-`/`+` chord was cycled from off to the maximum and
back, twice, while reading the `frontlight` selector. The scale is `0` (off)
plus ten levels of `10`, and it wraps around: `+` past `100` returns to `0`,
and `-` from `0` goes to `100`.

| `frontlight` | Level |
| ---: | --- |
| `0` | Off |
| `10`..`100` (step `10`) | `1`..`10` |

Each step emitted the frontlight command `5FF509VV` (the new value) twice, and
`frontlight_mode` stayed unchanged. The tray labels the levels `Off`, `1`..`10`
and clamps values above `100` to level `10` since 2026-09-28; the earlier
project-invented presets (`60`, `125`, `190`, `255`) were not the panel's
scale.

The same session showed that a lamp press applies a bundle of mode, level and
temperature. The observed pairs were `cold`: level `0`, temperature `100`;
`warm`: level `10`, temperature `0`; `off`: level `0`, temperature `70`;
`mixed`: level `10`, temperature `70`. Whether the level is stored per mode is
not established.

### Confirmed speed calibration

Holding `M` and pressing `+` changed speed `1 -> 2`; holding `M` and pressing
`-` changed it back `2 -> 1`. Speed is therefore reachable from the physical
controls, not only from software. The official macOS client lists five labels
(`Fast` through `Fast++++`) and the Paperlike 253 manual documents five speed
levels. Which label corresponds to which numeric value, and whether `+` selects
a faster refresh or a denser ink drop, has not been confirmed on this monitor.

Three tests on 2026-09-28 characterized speed further:

- **Speed does not depend on the display mode.** With speed set to `5`, `1`,
  and `3`, every read-back in `auto`, `text`, `graphic`, and `video` returned
  the value just set; a separate on-panel test pressed `M` four times
  (`3 -> 4 -> 1 -> 2 -> 3`) and speed stayed `1` throughout.
- **Speed survives a power cycle.** Speed `3` was written over serial, the
  monitor was powered off and on (its HID interface re-enumerated in the
  kernel log), and speed still read `3` afterwards.
- **The physical chord works and announces itself.** With speed at `4`, a
  `+`, `-`, `+`, `-` sequence with `M` held read back
  `4 -> 5 -> 4 -> 5 -> 4` while the mode stayed `text`. Each press emitted the
  speed command `5FF504VV` (the new value) twice, after a few seconds during
  which the serial interface did not answer. `+` raises the numeric value and
  `-` lowers it.

The same session showed that a physical `M` press emits the applied mode
command (`5FF5020X`) twice plus a contrast command (`5FF5010X`) once, and that
a serial mode write re-applies the target mode's stored contrast with the same
contrast notification; see the next section.

The tray labels the five values with the official client names in combo order
(`1` `Fast` .. `5` `Fast++++`) since 2026-09-28. That mapping is
official-client static evidence, not monitor-confirmed.

Third-party references describe the same scale as a blend of refresh speed and
ink blackness. The `paperlike-go` CLI for the 2019 Paperlike HD maps values
`1..5` to `Fast++`, `Fast+`, `Fast`, `Black+`, `Black++`; Dasung's own Chinese
material calls the `M` + `-`/`+` setting "ink-drop blackness" (parenthetically
"understood as adjusting speed") with five levels, and a 2021 review of the
HD-FT describes the trade-off as "the blacker the ink drop, the higher the
contrast and the slower the response", recommending `Black++` for static
documents and `Fast+` for editing. The Paperlike 13K init script calls command
`0x01` "speed/threshold" with options `1..8`. On the official-client side, the
Windows speed builder skips the write entirely when a per-port monitor-type
flag is set (it logs `monitor type specified, not send speed`), so support for
this parameter is not universal. How each level maps on this 2024 unit remains
unconfirmed, and an on-panel A/B comparison by the maintainer on 2026-09-28
did not show a visible difference in text or video mode.

### Observed per-mode contrast re-application

On 2026-09-28 every mode change was observed to re-apply a stored contrast
value and announce it on the wire. A serial mode write emitted the contrast
command (`5FF5010X`) twice; the physical `M` button emitted the mode command
(`5FF5020X`) twice plus the same contrast command. During the session the
announced value was stable per target mode (`text` `6`, `graphic` `5`,
`video` `6`) and matched the contrast read back after the switch.

Contrast writes over serial changed the live read-back immediately, but the
value re-applied by a later mode change did not always match the last value
written: writes of `3`, `5`, `8`, and `9` while in `text` were followed by `6`
on the next switch to `text`. The update rule for the stored value is not
established; what is established is that a mode change can override the live
contrast. The tray's restore order already writes contrast after mode, and
`Reload from monitor` re-reads it.

### Confirmed settings persistence

Contrast, mode, speed, frontlight mode, temperature, frontlight level, and mux
all survived a full power cycle unchanged. They are stored settings rather than
volatile state.

## Reference information and controlled tests

paperlike-rs associates these IDs with commands:

| ID | Referenced operation | Status here |
| ---: | --- | --- |
| `01` | Contrast | Read confirmed |
| `02` | Mode | Read confirmed |
| `03` | Refresh | Soft frame transmitted once; visible refresh confirmed |
| `08` | Frontlight temperature | Read confirmed |
| `09` | Frontlight level | Read confirmed |
| `0A` | Get parameter | The observed read marker; broader meaning unconfirmed |
| `10` | Protocol/version query parameter | Read confirmed |

paperlike-rs provides this refresh packet:

```text
5FF50300000000000000A0FA
```

On 2026-09-18 the project sent this frame exactly once in a controlled test.
The serial write completed, the user observed a visible screen refresh, and
all five confirmed read queries succeeded immediately afterwards with
unchanged values. That send-only test did not read the serial input, so no
acknowledgement was observed; the later acknowledgement tests above show that
implemented writes do answer. The
test confirms a visible refresh but does not yet characterize its waveform,
duration, affected region, or ghost-removal behavior. No meaning should be
invented for its zero payload.

On 2026-09-25 the hard-refresh option `01` was sent once on the target as an
approved targeted test: the monitor acknowledged the frame
(`5FF50301000000000000A0FA`) and the user reported a clean screen with no
ghosting. It did not measurably outperform the soft refresh for the light
ghost used in that test; details and photos are summarized in
[`ghosting-experiments.md`](ghosting-experiments.md).

## Official-client static evidence

This section records code found in official client binaries. It is not
monitor-confirmed behavior and does not authorize sending these requests.
The clients themselves are not distributed with this project; the distilled
evidence is in [`research-findings.md`](research-findings.md).

All three analyzed clients construct ordinary requests with:

```text
%.04X%.2X%.2X%.12X%.4X
```

Their arguments are prefix, command, option/value, literal integer zero, and
tail. Thus their ordinary command builders always render bytes 4-9 as zero.

The current Windows v2 client exposes separate refresh actions that construct:

```text
soft refresh: 5FF50300000000000000A0FA
hard refresh: 5FF50301000000000000A0FA
```

The macOS v2.0.3 hard-refresh action constructs the same option-1 form. No
refresh path in these clients supplies a non-zero payload or screen geometry.
This is evidence about these client implementations, not proof that the
controller lacks a regional operation.

The official Linux client source published for the 253 (retrieved
2026-09-25) uses the same framing format and command set (`01`, `02`, `03`,
`04`, `05`, `0A`, `10`) and sends only the global `5FF50300...` refresh. It
also defines an inbound remote-control event family after `5FF5 F5` whose
byte 3 carries event IDs, matching the macOS `F5 20` parser; it carries no
screen geometry.

This table is the frame reference recovered from the static evidence above; it
does not promote them to monitor-confirmed behavior. The tray sends contrast,
mode, refresh, speed, frontlight mode, temperature, and frontlight frames;
clock, mux, text enhancement, and dithering remain documented but are not
exposed by the current code:

| Operation | Frame |
| --- | --- |
| Contrast `V` (`1..9`) | `5FF501VV000000000000A0FA` |
| Mode (`auto`, `text`, `graphic`, `video`) | `5FF5020V000000000000A0FA`, `V=1..4` |
| Soft refresh | `5FF50300000000000000A0FA` |
| Hard refresh | `5FF50301000000000000A0FA` |
| Speed `V` (`1..5`) | `5FF504VV000000000000A0FA` |
| Clock fields | `5FF505HHmmssYYMMDD00A0FA` |
| Frontlight mode byte `V` | `5FF507VV000000000000A0FA` |
| Temperature byte `V` | `5FF508VV000000000000A0FA` |
| Frontlight byte `V` | `5FF509VV000000000000A0FA` |
| Mux byte `V` (semantics unconfirmed) | `5FF50BVV000000000000A0FA` |
| Text enhancement `V` (`0`/`1`) | `5FF512VV000000000000A0FA` |
| Dithering `V` (`0`/`1`) | `5FF520VV000000000000A0FA` |

Command `0x0C` (firmware-update state) is deliberately not exposed. The
frontlight-mode and mux bytes take a full byte because their value semantics
have not been recovered; text enhancement and dithering accept only the
official clients' `0`/`1` options. The tray reads back only the selectors it
shows and persists (`01`, `02`, `04`, `07`, `08`, `09`), while the `doctor`
probes every selector this firmware answers: `10`, `01`, `02`, `03`, `04`,
`07`, `08`, `09`, `0B`, and `11`. Selectors `03` and `11` have no established
meaning and are shown under neutral names. Selector `12` (text enhancement)
is not answered by this firmware, so the doctor reports it as a warning
instead of a failure.

On 2026-09-18 the target was tested with the official-client text-enhancement
and dithering frames. Reads `5FF50A12...` and `5FF50A20...` produced no
response, and writes `5FF51200...`, `5FF51201...`, and `5FF52000...` produced
no `F0 CC` acknowledgement, while implemented commands answered immediately.
One unsupported write returned an unrelated stale frame instead of an
acknowledgement; only `F0 CC` frames are treated as acknowledgements. The
supported conclusion is that commands `0x12` and `0x20` are not implemented by
this monitor's firmware, which is consistent with Dasung introducing text
enhancement with the newer 13K-series models. This is absence on this
protocol-`0x30` unit; it is not proof about any other model or firmware. The
`frontlight-mode` read (`07`) responded with value `2`.

A former raw-send command accepted one complete frame after validating its
length, hexadecimal encoding, prefix, and tail; it was removed on 2026-09-26
together with the rest of the command-line interface.

The archived Windows v1.2.6.09 client contains a real-time-clock path that
constructs this command from `GetLocalTime`:

```text
5FF5 05 HH mm ss YY MM DD 00 A0FA
```

`YY` is the local year minus 2000. In this command, byte 3 carries the hour and
bytes 4-9 carry minute, second, year offset, month, day, and zero. This is the
first identified non-zero use of the six-byte field. It demonstrates that the
field can hold command-specific data, but it provides no evidence for a
coordinate or rectangle layout.

The project's former clock command sent this layout with the host's local time
by default and accepted explicit values; it was removed on 2026-09-26 with the
rest of the command-line interface, and the frame remains documented for
reference.

## Unknowns and hypotheses

- `0x30` appears to be the protocol version, based on the corresponding field
  in paperlike-rs, but the monitor itself has not supplied a semantic label.
- The following `0x10` in the version response is a separate confirmed byte.
  Its meaning must be investigated and must not be inferred from its value.
- The meaning of response byte `0xF0` is unknown.
- The general purpose of request bytes 4-9 remains command-dependent and
  incompletely mapped. They carry clock fields for official command `0x05`,
  are zero in every confirmed read, and are zero in the official refresh paths
  inspected so far.
- Mode values `1..4` are confirmed. Contrast follows the official clients'
  nine-step cycle and was observed stepping `5 -> 6 -> 5` physically; its
  physical limits have not been swept. The frontlight level is a full byte in
  the frame; the panel's own scale is `0` off plus ten levels `10..100`,
  reachable with the lamp + `-`/`+` chord, and the tray clamps to it.
- Frontlight mode values are calibrated to the official labels: off `0`,
  cold `1`, warm `2`, mixed `3`. The project's `custom` value `4` is not a
  firmware mode: it reads back as `3` (mixed), with the temperature applied.
  The `temperature` field spans `0..100` (`100` cold, `0` warm) and the
  firmware clamps higher bytes; the tray's ten levels interpolate evenly
  between the endpoints.
- Serial writes of frontlight mode (`07`) were not observed to update
  `temperature` on this unit, unlike the physical lamp button, and reading
  `temperature` right after such a write stalls. The tray therefore sends a
  temperature before the mode and does not read back: `100` for cold, `0` for
  warm, and `70` for mixed. Off leaves the stored balance alone; custom reuses
  the last temperature set manually in the session. The tray's periodic read
  later shows the applied temperature, clamped by the firmware at `100`.
- Speed is a five-level value reachable with `M` + `-`/`+`, independent of the
  display mode, and preserved across power cycles. The project labels values
  `1..5` with the official `Fast`..`Fast++++` names in combo order; `+` raises
  the numeric value and `-` lowers it (`4 <-> 5` observed), but how each level
  changes the panel (refresh rate versus ink density) is not confirmed.
- A mode change re-applies a stored contrast value for the target mode on this
  firmware, and the physical `M` button announces it; serial contrast writes
  change only the live value. See "Observed per-mode contrast re-application".
- Selectors `03`, `04`, `07`, and `0B` respond on this firmware, while `12`
  and `13` do not. `04` and `07` are calibrated; `03` reports a live value
  (`1` then `0`) and `11` reads `2`, both with unknown meaning, so `info` shows
  them under neutral names.
- `mux` (`0B`) read `1` for most of the session and `3` later without an
  identified cause; no physical control changed it, and it persisted across a
  power cycle. Its meaning is unresolved. The next cheap test is switching the
  monitor between its Mini-HDMI and Type-C video inputs while reading `0B`.
- Commands `12` (text enhancement) and `20` (dithering) are not implemented by
  this firmware. Writes for `01`, `03`, `04`, `05`, `07`, and `0B` are
  acknowledged.
- One unsupported write returned an unrelated stale frame rather than a valid
  acknowledgement; the origin of that frame is not understood.
- No checksum field has been identified in the confirmed 12-byte packets.
- Behavior for malformed, unsupported, or state-changing commands is unknown.
- The vendor HID interface and CH341 device may have control roles, but neither
  has been investigated by this project.

Unknown commands must not be fuzzed. State-changing commands, including the
reference refresh packet, require explicit approval before being sent to the
physical monitor.

The long-term research goal is to determine whether the original controller
supports regional refresh or clear, regional waveform/update modes, or
different update modes in different regions. Evidence must come from official
client analysis, passive captures, or separately approved targeted tests; the
six-byte payload alone is not evidence of coordinate support.

The official Windows client updates the MCU through the CH341 programmer
(`ch341prog.exe`, Intel-HEX/`.bin` files) and bundles no firmware image, so an
image obtained from official support is currently the only offline source that
could confirm or rule out regional support without writing to the monitor.
