# Peripheral devices (follower, laser, AF, EC, pendant)

Reverse engineered from `Module/NCModule.dll` (base 0x10000000), `PHBX.dll` (0x10000000),
`MainApp.exe` (0x00400000), `File/ipAdd.ini`, `Help/calib.csv` and `xhcusb.txt`.
Tags: **[O]** observed in code/data (address given), **[I]** inferred, **[U]** unknown.
Function addresses are NCModule unless prefixed (PHBX / MainApp).

Code: `mlaser/devices/` (`codecs`, `transport`, `modbus_rtu`, `follower`, `laser`, `autofocus`,
`ethercat`, `pendant`), compatibility names in `mlaser/controller/devices.py`, UI in
`mlaser/ui/ext_devices.py` (`register(window)`). Tests: `tests/test_devices_*.py`.

## 1. Endpoints and transports

| Device | ini keys (shipped value) | Codec / framing | HAL | Evidence |
|---|---|---|---|---|
| Motion card | `CardIP:CardPort` 10.1.1.168:502 | CExtModbus (docs/protocol.md) | CMCHalAPI | [O] |
| On-board follower | MC registers 10000+/11000+; `OnBZFIP:OnBZFPort` 10.1.1.168:999 read but only used as the EC endpoint for card variants 5/6 | CExtModbus | MC HAL | [O] FUN_1004f3e0, FUN_1004ed60 |
| Network follower | `ZFIP:ZFPort` 10.1.1.169:502 | CStdModbus | none | [O] codec has **no users** in this build |
| Laser IPG (control type 2 "net") | `LaserIP:LaserPort` 10.1.1.170:10001 | raw bytes (CIPGModbus) | CLaserNetHalAPI | [O] FUN_10058b70, FUN_10010cd0 |
| Laser Raycus (control type 1 "MCC serial") | `OnBLaserIP:OnBLaserPort` 10.1.1.168:888 | raw bytes (CRaycusModbus) | CLaserNetHalAPI | [O]; [I] the card forwards it |
| Laser Raycus (control type 4 "PC serial") | serial port | raw bytes | CSerialHalAPI | [O] FUN_1004cf70 |
| AF / AdvAF | `AFIP:AFPort` :888 / `AdvAFIP:AdvAFPort` :666 (when `sys+0x4ad4`) | Modbus RTU slave 1 (CSerialModbus) | CAFNetHalAPI | [O] FUN_1001dc20, FUN_1004ed60 |
| EC / EC3710 | `ECIP:ECPort` :888 (card variant 3), `EC3710IP:EC3710Port` 10.1.1.170:502 (variant 1); `AdvEC*` only in the ini | Modbus RTU slave 2 (CExtCardModbus) | CECNetHalAPI | [O] FUN_1001ff30, FUN_1004ed60 |
| Pendant | USB HID VID 0x10CE | HID reports | PHBX.dll | [O] PHBX 0x100018c0 |
| Cloud monitor | `MonitorIP` 47.104.17.21:9001 | CMonitorModbus `A5 A5 .. 5A 5A` | CMonitorHalAPI | **omitted on purpose** |

**Sockets [O]** (FUN_100065a0, shared by AF/EC/laser HALs): AccessType 0 → TCP `connect` +
SO_SNDTIMEO/SO_RCVTIMEO/SO_LINGER; 1 → UDP (shipped). Always `sendto`/`recvfrom`. Retry knobs
per HAL (slot 7, FUN_100067a0 / FUN_10006820): max sends, max receive waits, sleep between
attempts; ini `AFTimeout/AFMaxSendTime/AFMaxRecvTime/AFSendInterval` etc. Note that the code
defaults differ from the ini (e.g. `LaserTimeout` default 1000, ini 100).

**Serial [O]** (FUN_100068a0 → CSerialPort::Open FUN_1002afc0): `\\.\COM%d`, baud index into
`[9600, 19200, 38400, 57600, 115200]`, parity argument 2 → NOPARITY (switch maps 0→EVEN,
1→MARK, 2→NONE, 3→ODD, 4→SPACE), 8 data bits, ONESTOPBIT, no flow control: **8N1**.
`ReadIntervalTimeout = SerialTimeout`, `ReadTotalTimeoutConstant = 10*SerialTimeout`.
Serial reads are fixed length: `2*count+5` for RTU reads (FUN_10010f20), 8 for RTU writes
(device types 1/9), 10 for type 10, 3 for Raycus (type 5), 14 for Raycus status (type 6)
(FUN_10011170). `PurgeComm(0xc)` before each write. A "no-reply" flag (HAL+0x28, set by
FUN_10006a30) makes the write return right after sending.

**HAL device types [O]**: 0 = MC (CExtModbus), 1/9/10 = CSerialModbus, 2 = IPG raw, 5/6 = Raycus
raw (6 = 14-byte `AA 55 … 55 AA` frame), 0xb = CExtCardModbus.

## 2. Codecs (vtables from RTTI) [O]

See the table in `mlaser/devices/codecs.py`. Summary:

- **CSerialModbus** (FUN_10028950/FUN_1002a110) and **CExtCardModbus** (FUN_10028c50/FUN_1002a320):
  standard Modbus RTU, slave 1 resp. 2, standard CRC (tables 0x1008a580/0x1008a680, low byte first;
  `01 03 00 00 00 02` → `C4 0B` verified in tests). Functions 0x03 (read), 0x10 (write) and the
  vendor 0x26 (`addr u32 LE, count u16 LE, bytes`). Decoders check CRC and slave id; errors
  -101 (short/CRC), -102 (byte count), -103 (slave).
- **CStdModbus** (FUN_10028740/FUN_10029f90): `00 00 00 00 | 2*len(vec) BE | 00 func | addr | count
  [| bc | words]`. The length is one short of real Modbus/TCP for writes. **Unused**: the singleton
  0x100acb60 is only referenced by its atexit destructor FUN_10083970.
- **CFTC61Modbus** (FUN_10028f50/FUN_1002a530): an RTU frame (encoded by the CSerialModbus
  singleton) wrapped as `00 00 00 00 | n+4 BE | 00 21 | n LE | rtu`; replies parsed like the MC
  codec (0x20 byte replies). **Unused** (singleton 0x100acb70 only in FUN_100839b0).
- **CIPGModbus / CRaycusModbus**: bytes copied verbatim; IPG needs ≥1 request byte and ≥3 reply
  bytes, Raycus ≥3 request bytes.
- 32-bit values on RTU devices are two 16-bit words, **low word first** (FUN_1004b510 writes
  `v & 0xffff, v >> 16`; FUN_1004bee0 reads `w1<<16 + w0`).

## 3. Height follower (ZF)

The live path in this build is the follower **inside the motion card** (registers via the MC
codec, 32-bit):

| What | Frame | Evidence |
|---|---|---|
| status | read 18 regs at 10000 → `getReg(7, i)` (VM+0x36c) | [O] FUN_1004f3e0 (only when `sys+0x4994` ≠ 0) |
| properties | read 41 regs at 11000 → `getReg(8, i)` (VM+0x3b4); sets `sys+0x4a98=20000`, `sys+0x4a9c=200` first | [O] FUN_1004f720 |
| follow on/off | mailbox 101 `[118, 4, on, int(sys+0x4dd0)]` (needs `sys+0x4dcc` and `sys+0x4994`) | [O] FUN_10041740 (VM slot +0x80) |
| calibration "ZF Signal correction" | mailbox `[118, 7]` (needs follower present, VM+0x42c==1) | [O] FUN_10041630 |
| Z "signal go origin" | 7 words to MC reg **309** → dropped by the HAL write filter (dead) | [O] FUN_10037c10 (CNCModule +0x60) |
| byte download | MC func 0x26 `[0x26, addr, n, bytes]`, n ≤ MaxZFDownItem (512) | [O] FUN_10053c70; [I] firmware/table |
| flag | FUN_100394b0 tests `syncReadZFReg(10004) & 0x80000000` | [O] |

MainApp ZF view (needs `ZF.ZFType` = `sys+0x4994` ≠ 0; 0 none, 1 on-board FTC) [O call sites,
semantics from labels I]:

| getReg(7, i) | label (`ZFReadOnly01..17`, `A251103_4`) | scale |
|---|---|---|
| 0 | follower info | int |
| 1 | alarm status | int |
| 2 | running status: **bit 31 homed**, low byte calibration grade 0x10 excellent / 0x11 good / 0x12 bad | bits |
| 3 | run command (**107** while head calibration runs) | int |
| 4 | signal strength (bit 15 = the FUN_100394b0 flag) | int |
| 5 / 6 / 7 | signal height / axis position / current speed | µm ×0.001 (speed ×0.1) |
| 8 / 9 | following error / follow time | int |
| 10 | plate coordinate | ×0.001 |
| 11 / 12 | real-time signal / signal compensation | int |
| 13 | soft lower limit | ×0.001 |
| 14 / 15 / 16 | edge-seek samples / max-deviation point / last position (×0.001) | |
| 17 | signal fluctuation | int |

The status-bar Z shows `reg(7,6)×0.001` (MainApp FUN_005168c0). Inch mode divides by 25.4.

Follower commands from MainApp [O]:

| Action | Write | Evidence |
|---|---|---|
| head calibration | `writeReg(101, 107)` (refused unless reg(7,2) bit 31) | MainApp FUN_0062cd50 → NC +0xf0 |
| cancel | `writeReg(101, 101)` | FUN_0062cd50 / FUN_0062d1c0 → NC +0xe8 |
| servo calibration | `writeRegs(101, [107, app+0xbc28 × 10])` | FUN_0062d240 [I] |
| write property | `writeReg(11000 + 2·idx, v)` then `writeReg(150, 9999)` | FUN_00633000 |
| factory reset | `writeReg(150, 5555)` (UI password "1273" not reproduced) | FUN_00633a10 |
| calibration curve | n = reg 12000, then 2n+2 regs from 12002 (chunks ≤ 200), paired | VM +0xcc FUN_10042cf0, MainApp FUN_00633da0 |
| test data | count at 10028 (two halves), records → csv `"%d, %d, %d, %.3f"` | FUN_00634070 → VM +0xdc |
| `.zpf` backup | export: `N\r\n` + N × `%d\r\n` + `eof` from 11000; import: func 0x40 at 11000, 2000 ms timeout | FUN_006343c0 / FUN_00634780 |

Note the property write address `11000 + 2·idx` against the read `getReg(8, idx)` = register
`11000 + idx`: both observed; whether the card maps them consistently is [U]. Property table
(`getReg(8, idx)`, scale in brackets) is `PROP_FIELDS` in `follower.py`: 0 empty-move speed, 1
acceleration, 2 acceleration time [1000], 3 follow tolerance [1000], 4 drill tolerance [1000], 5
home on power-up, 6/7 home fast/fine speed, 8 origin offset [10], 9 calibration height [10],
10/11 calibration coarse/fine speed, 12–14 calibration signal parameters, 15/16 soft low limits
[1000], 17 maximum effective signal (200000..1000000), 18–26 collision/touch and following-error
alarms (touch-sheet alarm signal, saturation, delays, abnormal capacitance, follow-error alarm
height [1000] and delay, alarm keep time), 27/32 filter factors, 28 touch→auto lift, 29
zero-signal alarm, 30 dynamic signal correction, 31 signal compensation, 33–37 follow KP, KI,
low-pass, integral saturation, decel time, 38 sensitivity (0..30), 40 fluctuation alarm.
**Collision and overrange** are therefore card-side alarms configured by props 18–26 and
reported in status word 1 (bit layout [U]); MainApp messages gp33–gp43/gp59.
Follow/lift heights are per-layer CAM parameters (`GP.CutHeight`, `GP.UpHeight`, …) consumed by
the process items, not follower registers.

**Quirk [O]**: syncReadZFReg (FUN_1004ae90 @0x1004af75) computes `vec[3] << 16 + vec[2]` on the
MC reply vector `[func, addr, count, r0, r1]`, i.e. `(r0 << 16) + 2`. The flag test therefore
really tests **bit 15 of register 10004**. The rebuild reproduces this (`OnBoardFollower.read_reg32`,
`FollowerStatus.flag`).

Calibration table `Help/calib.csv` [O]: written by MainApp FUN_00633da0 with
`fprintf("%d, %d\n")` in text mode (CRLF) from the card's curve (registers 12002+). 64 lines:
first the header pair `413023, 0` (registers 12002/12003), then `0, 406507`, `0, 409718`,
`101, 410429`, … `4201, 412318`. [I] the header is the far-field reference signal; the rest are
`(height in µm, raw signal)`.
`CalibrationTable` parses/writes it byte-identically and interpolates.

Network follower (`NetworkFollower`, CStdModbus over TCP [I]) is provided for completeness only.

## 4. Laser source [O]

Command bytes from the protocol adapters (Raycus vtable 0x1008a52c, IPG 0x1008a4f4):

| Slot | IPG | Raycus |
|---|---|---|
| +04 emission on | `EMON\r` (FUN_10027260) | `1B 4F 0D` (FUN_10027110) |
| +08 emission off | `EMOFF\r` (FUN_100272b0) | `1B 53 0D` (FUN_10027140) |
| +0c / +10 aiming on/off | `ABN\r` / `ABF\r` | empty |
| +14 power | no-op | `1B 43 50 pp 0D`, pp raw byte ≤ 100 (FUN_10027170) |
| +18 pulse | no-op | 18 zero bytes if 50 ≤ f ≤ 50000, a,b ≤ 100 (stub, FUN_100271f0) |
| +1c | `EMOD\r` | no-op |
| +20 | `SDC<n>\r` (decimal, no space; FUN_10027880) | no-op |
| +24/+28 | `ELE\r` / `DLE\r` | no-op |
| +2c/+30 | `DEABC\r` / `EEABC\r` | no-op |

Only +04/+08 (CNCModule +0x160/+0x164, +0x184/+0x188) and IPG +0c/+10 (CNCModule
+0x174/+0x178, together with the red-light output) are called. **No status, alarm or
temperature query exists** (no `STA`/`RCT` etc. in any string or immediate); the original never
reads anything back except "non-empty reply". `IpgLaser.query()` can send vendor commands
such as `STA` [I, untested, not in Mlaser].

Parameters (names from MainApp): `LGP.LaserType` = `sys+0x49a0` (0 Raycus, 1 IPG, 2
semiconductor, 3 MaxPhotonics, 4 Super, 5 TXStar, 6 nLight, 7 GZ, 8 others) and
`LGP.LaserControlType` = `sys+0x49a4` (0 none, 1 MCC serial, 2 net, 3 IO, 4 PC serial).
vmctLaserWrite FUN_1004cf70 (VM +0x9c): commands go out only for Raycus with control 1 (network
HAL to OnBLaser 10.1.1.168:888, i.e. the card's serial port) or 4 (PC COM port), and IPG with
control 2 (10.1.1.170:10001). Control 3 = DO `LGP.DOLaserGate` + DA (`LC.PtLaserPeakCurrent`;
LaserType 7 scales DA by 0.95, 0x1008ad90). The red-light key toggles DO `LGP.DORedLight` and,
for IPG, `ABN`/`ABF`. Laser alarms shown by MainApp (gp44 serial, gp46 temperature, gp49 power,
gp50/52 interlocks, gp53/54 boards, gp58 not connected) are raised inside NCModule from a source
that was not found [U]. In the "fire and forget" state (VM+0x11e8) the command
is sent 3 times with 1 ms sleeps and the reply is ignored.

Laser power during cutting stays on the motion card (DA `[9999,4,ch,v]`, PWM `[9999,3,f,d]`).

**Laser unlock** [O, omitted]: MainApp FUN_00482130 (gp122, alarm gp121 "Laser is Locked")
→ NC +0x18c → VM +0xa0 FUN_1004d280 sends `AA 55 00 1A 00 FF 14 03 <code> <cs/255> <cs%255> 55
AA` (32 bytes) over the Raycus channel; reply byte 7 + 10000 → 10001 ok, 10002 verify error,
10003 length error, 10004 locked, 10005 n tries left. It unlocks the laser vendor's rental/expiry
lock, so per the brief it is documented and not implemented.

## 5. Autofocus (AF / AdvAF) [O unless noted]

- RTU slave 1 over UDP; AF type `sys+0x4ac8`: 1 → HAL VM+0x2c, 2 → HAL VM+0x28 (network);
  3/5/6 → via the motion card (MC read of 14 regs at **13000** status, **13200** props); 0 = none.
- syncReadAFReg FUN_1004bee0 (VM +0x8c): `[3, addr, 2]` → `w1<<16 | w0`.
  syncWriteAFReg FUN_1004b510 (VM +0x88): `[0x10, addr, 2, lo, hi]`, retried every 30 ms while
  the reply mismatches. syncWriteAFRegs FUN_1004b8d0 (VM +0x84) writes vectors.
- Status (updateAFStatus FUN_1004f980): 28 words at 1000 → 14 values `getReg(9, i)`.
  Properties (updateAFProp FUN_100503e0): words at 2000 → `getReg(10, i)` (count [U]).
- MainApp commands [O]: register 101 ← 1 stop, 2 home, 5 [U], `[4, pos_mm×1000, speed×1000]`
  absolute move (jog up/down = current ± step), `[3, x×1000, 0]` [U]; register 100 ← 9999 save /
  8888 handshake; register 2020 = origin offset (limit 18000). Status `getReg(9, i)`: 0 model, 1
  software version, 2 DI, 4 alarm, 5 run status (bit 31 homed), 6 command, 7 **focus position
  ×0.001 mm**, 8 speed ×0.001, 9/11/13 ×0.001 and 10 ×0.1 [U, I: pressure/temperature].
  `AF.AFType` UI values: 0 none, 1 MCC serial, 2 net, 3 DA, 4/7 on-board.
- "FTC Upgrade" (`hw.upgrade_ftc`, fillAFDownFile FUN_10053fb0, `MaxAFDownItem=200`): described
  only.

## 6. EtherCAT / extension card (EC) [O]

- RTU slave 2 over UDP. Card variant (FUN_1004ed60): `RO[1]/10000` if `RO[1]%10000 ≥ 6000`, else 3
  if `(RO[0]>>17)&3 == 2`, else 1.
- Address rule (FUN_1004c280/FUN_1004c640/FUN_1004cc20): `+50000` when variant is 1 or ≥ 5, or
  `sys+0x4ac8 == 3`; otherwise unchanged and the HAL waits 500 ms after sending.
- Mailbox register 101:

| CNCModule slot | Words | Function |
|---|---|---|
| +0xb0 jog | `[4, speed*1000, length*1000]` ("EC Jog: Speed %.2f, Length %.2f") | FUN_10037e70 |
| +0xb4/+0xb8 pulse jog | `[4, speed, length]` | FUN_10038030 |
| +0xbc stop | `101 := 1` | FUN_100381c0 |
| +0xc4 go origin | `[2, sys+0x4bbc == 1]` | FUN_100382b0 |

- Status (updateECStatus FUN_10050900): 30 words at 1000 (51000) → 15 values `getReg(11, i)`.
  Properties (FUN_10050c70): `2n` words at 2000 (52000) → `getReg(12, i)`.
- MainApp also uses `[7]` (+0xc8 [U]), PWM `writeECReg(104, f)` / `(105, duty)`, register 103
  (+0xcc), `writeECReg(100, 8888/9999)`. Status `getReg(11, i)`: 0 product, 1 version, 2 DI, 3 DO,
  4 alarm, 5 run status (bit 31 homed), 6 command, 7 position ×0.001, 8 speed ×0.001, 9/10 PWM.
- "false init 3710 ExCard" FUN_100546e0: initialisation sequence for the EC3710 card [U].

## 7. Handheld pendant (PHBX.dll) [O]

- VID 0x10CE, 20 PIDs (0xEB61…0xEBAA) mapped to model codes (XOpen 0x100018c0). Interfaces are
  told apart by caps: FeatureReportByteLength 8 → control, InputReportByteLength 6 → input.
- `xhcusb.txt` lines: "Open HID error5/32" = CreateFile on foreign HID interfaces (access
  denied / sharing violation), "Get HID Error!1168" = HidD_GetAttributes ERROR_NOT_FOUND. The
  log never shows "open xhc usb device OK", i.e. no pendant was attached [I].
- Input `04 S k1 k2 A C`; checksum with `c=~S`, `d`=UTC day: `((k1+c)^(d|c))==C` or
  `(((d|c)+c)^k1)==C` (FUN_10001380). Events go out as WM 0x4B1 (0x100 down/0x101 up) and the
  optional callback.
- Feature reports (id 6): day sync `06 FE FD dd FF FF 00 00`; LEDs `06 FE FD dd p0 p1 00 00`
  (XSendOutput, model 0x26C remap); pairing `06 FA FB dd lo hi 00 00` (XWritFid); status
  GetFeature → DevID, channel, RSSI (≥0x50 = poor signal), FID.
- **No MPG wheel, axis selector or step multiplier**: hold-to-jog keys. MainApp key map
  (FUN_005ab2e0): 0x13/0x11 X±, 0x0F/0x12 Y±, 0x10/0x0E Z± (Fn: W±), 0x01 follow, 0x02
  shutter, 0x03 red light, 0x04 laser while held, 0x05 gas, 0x06 frame, 0x07+Fn home, 0x08 start,
  0x09 pause/continue, 0x0A stop (the only key while busy), 0x0D high/low jog speed (Fn: step mode
  [I]), 0x0B = Fn. LED feedback for keys 1..6 (FUN_005961b0).
- Exports: Xinit, XOpen(HWND), XClose, XGetInput(&packed,&type), XSendOutput(data[2],&len),
  XWritDev, XWritFid, XGetVersion, XGetDevRssi, XGetDevID, XGetFID, XGetDevTxstate, XGetChannel,
  SetGetKeyCallbackFunction (all stdcall).
- Enabled when `SOP.RemoteType` (HardPara, app+0x4078, "手柄类型") == 3 "LCR19" (default);
  MainApp FUN_005b8d10 `LoadLibraryW("PHBX.dll")` → Xinit → XOpen(hwnd), polls XGetInput every
  200 ms (FUN_005cc160) for status only; keys arrive as WM 0x4B1. Types 0/1 "LCR20" (board / PC)
  are handled in FUN_005cdcd0 [U].
- Rebuild: `mlaser.devices.pendant` with hidapi (lazy import), `PendantJogMapper` maps events to
  `ControllerFacade.jog_start/jog_stop` and action callbacks.

## 8. Intentionally omitted

- Cloud monitor (CMonitorHalAPI / CMonitorModbus, 47.104.17.21:9001, JSON `devId`, `warnId`…).
- Licensing / dongle / "User Lock" (FUN_10042a90) and MainApp FUN_005cc630 (hardware id check).
- Firmware upgrades of AF/ZF/extension cards (described, not implemented).

## 9. Unknowns

- Bit layouts of follower status words 0/1 (alarms incl. collision/overrange), AF/EC alarm words,
  blocks 10, 13, 14; meaning of AF command 5 / `[3, …]`, EC `[7]`.
- Unit of the follow value `sys+0x4dd0`; the calib.csv header pair (I: reference signal).
- Source of the laser alarms in NCModule.
- Content of the 10-byte Raycus status frame (type 6) and who requests it.
- Pendant aux byte A, states 0x68/0x69.
