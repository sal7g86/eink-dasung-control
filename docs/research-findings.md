# Official-client static-analysis findings

Analysis date: 2026-09-18, with an addendum on 2026-09-25 covering the
official Linux client source and the 10.3-inch companion APK. All findings in
this document are **official-client static** evidence. No packets were
transmitted during this analysis; a later controlled soft-refresh test is
recorded in [protocol.md](protocol.md).

The official installers and their extracted files are **not distributed**
with this project (they remain outside version control); the artifact hashes
and download URLs recorded during the analysis and the machine-readable
command inventory are not shipped either. This document is the distilled
evidence.

## Scope and method

The following clients published on Dasung's official download page were
analyzed offline:

- Windows v2 (`DSPaperLikeClient.exe`, native 32-bit PE)
- macOS v2.0.3 (`PaperLikeClient`, universal x86-64/arm64 Mach-O)
- archived Windows v1.2.6.09 (`DSPaperLikeClient.exe`, native 32-bit PE)
- official Linux 253 client source (`DS253.c`, 2021; addendum)
- Android 10.3-inch companion APK (`app-product-103-debug`; addendum)

The installers were extracted without executing their contents. The macOS
package is an XAR containing a gzip-compressed CPIO payload. The Windows
installers use Setup Factory 7. `sfextract` 0.0.8 incorrectly skipped one byte
after the installer signature for these two files; extraction succeeded after
removing that single `overlay.seek(1)` from the disposable virtual-environment
copy of `setupfactory7.py`. The extracted executable hashes make this result
independently checkable. The addendum artifacts needed no extraction beyond
`unrar`: the Linux archive contains C source, a built binary, and a log, and
the APK was read with `unzip -p` and `strings` without decompilation.

The analysis used `strings`, GNU `objdump`, and LLVM `llvm-lipo`, `llvm-nm`,
`llvm-otool`, and `llvm-objdump`. A dedicated offline frame scanner (not
shipped in 0.1) also scanned all extracted files. It found no complete
constant 12-byte frames. Disassembly shows why: the applications construct
their ASCII-hex requests dynamically.

The macOS download and its user-facing version text say v2.0.3, while its
bundle metadata reports `CFBundleShortVersionString` 1.2. This is recorded as
a packaging discrepancy, not treated as a different artifact.

## Packet construction

The macOS client's central builder,
`-[UtilsORSSerialPortsValue sendCommand:withOptional:toDevice:]` at x86-64
address `0x100003125`, uses this exact format string at `0x100028e0b`:

```text
%.04X%.2X%.2X%.12X%.4X
```

The arguments are `0x5FF5`, command, option, integer zero, and `0xA0FA`.
Consequently, every request routed through this builder has 12 zero hex digits
in bytes 4-9. A second builder inside ORSSerialPort at `0x10000882b` uses the
same format and also supplies integer zero.

Both Windows executables use the same normal-command format. Their named
builders cover threshold (`0x01`), mode (`0x02`), refresh (`0x03`), speed
(`0x04`), frontlight mode/temperature/brightness (`0x07`/`0x08`/`0x09`), get
parameter (`0x0A`), mux (`0x0B`), firmware-update state (`0x0C`), text
enhancement (`0x12`), and dithering (`0x20`, current clients). The inventory
identifies the exact builder and call-site addresses.

Two additional frame families appear in receive/acknowledgement code. The
clients parse `F0 0A` parameter responses, matching the monitor captures in
this project, and the Windows clients can emit `F0 VV` acknowledgements with a
zero payload. The macOS parser also recognizes an inbound `F5 20` family and
dispatches it as a remote-control event. None of these paths contains regional
screen data.

## Mode tables

The macOS setter's broad `2..7` validation is not a six-mode list. Its
value/index conversion code supports five display-version profiles, each with
four UI entries:

| Display-version profile | Four raw mode values |
| --- | --- |
| `1` or `2` | `6, 2, 3, 7` |
| `3` | `5, 2, 3, 7` |
| `4` or `5` | `3, 4, 5, 2` |

Depending on profile, its resources label those entries with variants of
Web/Auto, Text, Image, and Active. Values `5`, `6`, and `7` therefore belong to
other profile mappings; they do not establish extra modes on the target.

Physical-control calibration on the protocol-`0x30` Revolutionary instead
confirmed `Auto=1`, `Text=2`, `Graphic=3`, and `Video=4`. This monitor-specific
evidence takes precedence over the multi-model client range. The client's
selector-`13` profile query received no response from the target within one
second.

## Non-zero six-byte field

The archived Windows client contains one confirmed dynamic use of bytes 4-9:
its `set_rtc` path calls `GetLocalTime`, uses command `0x05`, and formats:

```text
5FF5 05 HH mm ss YY MM DD 00 A0FA
```

Here `HH` occupies byte 3. The six-byte field at bytes 4-9 is minute, second,
year minus 2000, month, day, and zero. This interpretation comes directly from
the accessed `SYSTEMTIME` structure offsets in the code at `0x78c9d0`, not
from packet guessing. The current Windows v2 binary retains the RTC format and
`set_rtc` strings but has no code reference to that format. The macOS client's
RTC-related action sends command `0x05` with all remaining bytes zero.

This result disproves the narrower hypothesis that bytes 4-9 are always
padding. It also shows that the field is command-specific. It supplies no
evidence that the six bytes encode screen geometry.

## Refresh paths

The current Windows client has separate hard- and soft-refresh UI paths. Both
call the same command `0x03` builder:

```text
soft: 5FF50300000000000000A0FA
hard: 5FF50301000000000000A0FA
```

The macOS `hardRefreshAction:` also reaches command `0x03`, option `0x01`.
Its builder accepts only options 0 and 1 for this command. These originated as
static constructions. A later controlled target test confirmed that option
`0` causes a visible refresh; option `1` was sent once on 2026-09-25 and was
acknowledged (see [protocol.md](protocol.md) and
[archive/ghosting-experiments.md](archive/ghosting-experiments.md)).

No refresh call site in the three analyzed clients passes coordinates,
dimensions, a rectangle object, or a non-zero six-byte field. Searches for
region, partial update, dirty rectangle, coordinate, and waveform concepts
found no protocol-related implementation. Windows strings such as `Rectangle
Tool`, `CRectTracker`, and `InvalidateRect` belong to the bundled MFC UI
framework and do not lead to the serial builders. macOS `CGRect` and tracking
area references likewise belong to AppKit UI code.

The supported conclusion is limited: these official desktop clients expose
global soft/hard refresh requests and provide no static evidence of an
explicit regional refresh command. This does not prove that the controller or
firmware lacks a regional operation. Such an operation could be unused by
these client versions, implemented through another interface, or hidden
behind behavior that static analysis did not reach.

## Management commands

A second static pass on 2026-09-18 looked at the management-oriented commands
`0x04` (speed) and `0x0B` (mux).

The Windows v2 speed builder at `0x40dbe0` constructs `5FF504VV...` from a
per-port value and checks a per-port flag at `0x5f5da8` before sending. When
that flag is set, it logs `monitor type specified, not send speed` and returns
without transmitting. The macOS client populates its `speedInfo` combo in
`initViewInof` with five constant entries and stores values per port in
`curPortSpeedMutableArr`. This supports the interpretation that speed is a
per-input refresh-speed level with five options; it does not establish the
physical meaning of each level.

The Windows v2 mux builder at `0x40cc30` constructs `5FF50BVV...`. Its only
call site (`0x4136a9`) sits inside a loop over four port slots and passes
`selected_index + 1`. The Windows UI exposes `Current MUX state:` and
`Set MUX:`, and its dialog strings are `P0: NO MONITOR` through `P4: NO
MONITOR` inside the `DS25inchMonitorClient v0.8` interface. This supports a
multi-monitor slot selector (which attached device the client controls) rather
than a video input selector. The analyzed macOS client has no equivalent
control. The target monitor has Mini-HDMI and USB Type-C video inputs and
reported mux value `1` for most of the session and `3` later, with no
identified action causing the change and no physical control altering it during
calibration. The slot reading and a per-input interpretation are both
unresolved.

The macOS `frontLightModeInfo` combo is populated in `initViewInof` with four
UTF-16 labels (Chinese text): off, cold/white, warm, and mixed. The
`speedInfo` combo carries five labels: `Fast`, `Fast+`, `Fast++`,
`Fast+++`, and `Fast++++`.

A physical-control calibration on 2026-09-18 cross-checked these static labels
against the target (full details in [protocol.md](protocol.md)). The lamp button cycles the
frontlight mode `3 -> 1 -> 2 -> 0`, and the observed light matches the combo
order: off `0`, cold `1`, warm `2`, mixed `3`. Holding `M` while pressing `+`
or `-` changes speed by one step (`1 <-> 2` observed), so speed is a physically
reachable five-level value. Which label maps to which number and the direction
of the buttons remain unconfirmed.

## Official Linux client source (2021 addendum)

Dasung publishes a Linux client for the 253 as a small RAR archive that
contains the complete C source (`DS253.c`), a built binary, an execution log,
and a Python remote-control helper. The source confirms that this protocol
family uses the same prefix, tail, 115200-baud ASCII framing, and a single
generic setter with the same format string as the desktop clients:

```text
%.04X%.2X%.2X%.12X%.4X
```

Its command set is threshold `0x01`, mode `0x02`, refresh `0x03`, speed `0x04`,
RTC `0x05`, parameter read `0x0A`, and version `0x10`. The `-r` refresh action
sends only the global `5FF50300000000000000A0FA`; no coordinate, rectangle, or
partial-update concept exists anywhere in the source. Its RTC builder is the
only non-zero payload path and is inconsistent with the Windows one: it passes
`1900+tm_year` through `%.2X` and does not increment `tm_mon`, so the bundled
log records 25-hex-character frames that the 24-byte send truncates.

The receive loop defines an inbound remote-control event family after
`5FF5 F5`, with byte 3 carrying event IDs `01`-`17` for page/arrow keys, mouse
moves and holds, and six custom keys. This matches the macOS client's `F5 20`
inbound parser. These events carry no screen geometry.

The archive adds no regional refresh path, and no code path that could accept
one. It is recorded as official-client static evidence because it is published
source from the vendor.

## Android companion APK (addendum)

The 10.3-inch companion APK (`app-product-103-debug`) was scanned with
`unzip -p ... classes.dex | strings`. It contains no `5FF5`/`A0FA` frame
constants and no serial control code; it is not part of the monitor protocol
work and was not decompiled further.

## Firmware update path

The Windows v2 client contains MCU firmware-update code paths
(`DS25inchMonitorClientFWUpdate`, `DS25inchMonitorClientMCUFWUpdate`), strings
such as `AUTO MCU FW update`, `FW Update Hex:`, `Current Firmware Version:`,
`get_mcu_report`, and `.\updateMCU.bat`, and it ships `ch341prog.exe` with the
CH341 driver package. No firmware image is bundled in either Windows installer
or the macOS package: the update expects Intel-HEX/`.bin` files that, per user
reports, official support provides on request. The update path uses the CH341
programmer interface, not the serial `0x0C` firmware-update-state command.

Analyzing a firmware image offline is therefore the remaining way to decide
whether the controller implements regional updates without writing to the
monitor. Obtaining an image requires contacting Dasung support; no public image
for this model was found on the official download page as of 2026-09-25.

## Third-party references

Independent projects use the same protocol family and expose only the global
clear:

- `codefastdieyoung/paperlike-rs`: 13K (protocol `0x31` expected) with refresh,
  contrast, mode, and frontlight; no regional command.
- `cpmetz/dasung253`: Python 253 client using `5FF50300000000000000A0FA` for
  ghost clearing.
- `mcarr823/kotlin-dasung253-client-library`: 253 UART abstractions based on
  vendor reference code; parameters only.
- `jtbg/paperlike-go`: older HD models over I2C DDC; global clear only, and a
  five-level drawing-speed scale documented as `1` `Fast++`, `2` `Fast+`,
  `3` `Fast`, `4` `Black+`, `5` `Black++`.
- `kahne/paperlike13k_macos` (fork of `plateaukao/paperlike13k_macos`):
  Paperlike 13K serial init script; calls command `0x01` "speed/threshold"
  with options `1..8` and command `0x02` "display mode" with `1..6`.
- `simpleapples/InkControl`: macOS menu-bar client; one-click and timed global
  refresh only.

None reports or implements a regional refresh.

## Further evidence needed

The next conservative step is passive serial capture of the official client
on a supported host, correlating each UI action with its traffic. That would
promote the constructed packets from static evidence to runtime capture. A
cheap targeted follow-up for mux is switching the monitor between its Mini-HDMI
and Type-C video inputs while reading selector `0B`, which distinguishes a
per-input state from a slot selector without sending any write. Any targeted
test of command `0x03`, and any experiment with commands or values not already
observed, still requires explicit approval before monitor access.

As of 2026-09-25 the project host has no Windows environment, so the runtime
capture step cannot be performed locally. The remaining offline route is a
firmware image (see the firmware update path section); passive observation of
the CH340, the HID vendor interface, and the CH341/I2C bus is possible with
reads only. Until one of those produces a candidate frame, no regional refresh
frame can be built or tested. The project's first software zone-clearing
approximation (an X11 overlay that flashed only the changed screen areas and
never used the serial channel) was removed on 2026-09-26; a different,
estimate-driven version was reintroduced later (see
[ghost-estimate.md](ghost-estimate.md)), and the archived measurements of the
first attempt are in
[archive/ghosting-experiments.md](archive/ghosting-experiments.md).
