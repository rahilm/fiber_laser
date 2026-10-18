# MCC100 motion-card protocol (Mlaser v0.0.1.51_SP1)

Reverse engineered from `Module/NCModule.dll` (image base 0x10000000, Ghidra dump
`decomp/NCModule.c`, objdump disassembly), `File/ipAdd.ini`, `Log/2026-09-16.log` and
MainApp.exe call sites. Function addresses are written `FUN_xxxxxxxx` (NCModule unless
another module is named). **Observed** means read directly from code or data.
**INFERRED** means deduced from context. **UNKNOWN** means not recovered.

Implementation: `mlaser/controller/frames.py`, `client.py`, `simulator.py`, `devices.py`.
Tests: `tests/test_controller.py`.

## 0. Architecture in the DLL

| Class (RTTI) | vtable | Role |
|---|---|---|
| `CNCModuleProvider` | 0x1008b65c | returned by the only export, `newModuleProvider` (0x100374f0) |
| `CNCModule` | 0x1008b314 | public API used by MainApp (≈209 virtual slots, mostly forwarders) |
| `CVirtualMachine` | 0x1008cd24 | worker thread: event queue, status polling, FIFO building and filling |
| `CMCHalAPI` | 0x100880ac | motion-card transport: socket, retries, register read/write, address filter |
| `CAFNetHalAPI`, `CECNetHalAPI`, `CLaserNetHalAPI`, `CSerialHalAPI`, `CMonitorHalAPI` | 0x100880d4 … 0x10088174 | other devices |
| `CExtModbus` | 0x1008a844 | **frame codec for the motion card** (encode FUN_10028190, decode FUN_10029a90) |
| `CStdModbus` | 0x1008a79c | Modbus/TCP-like codec (encode FUN_10028740, decode FUN_10029f90) |
| `CSerialModbus` | 0x1008a7cc | Modbus RTU, slave 1, CRC FUN_10027af0 (encode FUN_10028950) |
| `CIPGModbus` / `CRaycusModbus` | 0x1008a7b4 / 0x1008a814 | raw byte pass-through (FUN_100288e0 / FUN_100290e0) |
| `CExtCardModbus`, `CFTC61Modbus`, `CMonitorModbus` | 0x1008a7e4 / 0x1008a7fc / 0x1008a82c | not analysed |

Codec singletons live at 0x100acb50… The motion card uses `0x100acb50` (`CExtModbus`, getter FUN_10027bb0).
MainApp gets the `CNCModule` object and calls slots such as `+0x1a0` = `getReg(block, index)`
(MainApp FUN_00481c70 uses it to store axis positions in `softPara.ini`).

## 1. Transport

| Device | Address (ipAdd.ini) | Transport | Evidence |
|---|---|---|---|
| Motion card MC | `CardIP=10.1.1.168`, `CardPort=502` | **UDP** when `[Soft] AccessType=1` (shipped value), TCP when 0 | FUN_100571f0 logs "Access: UDP"/"Access: TCP"; FUN_100062a0 `socket(2,2,0x11)` vs `socket(2,1,6)` |
| Network follower ZF | `ZFIP=10.1.1.169:502` | `CStdModbus`; **no users in this build** (singleton referenced only by atexit destructors; [devices.md](devices.md)) | FUN_10017d40 HAL, encoder FUN_10028740 |
| On-board follower | `OnBZFIP=10.1.1.168:999`, also MC registers 10000+ | via MC | FUN_1004ae90, FUN_1004f3e0 |
| Laser | `LaserIP=10.1.1.170:10001`, `OnBLaser…:888` | raw ASCII/ESC commands; channel by LaserControlType ([devices.md](devices.md)) | `CRaycusModbus`/`CIPGModbus` |
| AF / AdvAF | `10.1.1.168:888` / `:666` | Modbus RTU slave 1 over UDP ([devices.md](devices.md)) | `CAFNetHalAPI` |
| EC / AdvEC / EC3710 | `10.1.1.168:888` / `:666` / `10.1.1.170:502` | Modbus RTU slave 2 over UDP ([devices.md](devices.md)) | `CECNetHalAPI` |
| Cloud monitor | `47.104.17.21:9001` | HTTP/JSON | **omitted on purpose** |

**MC socket (FUN_100062a0, observed).** UDP: no `bind` and no `connect`. Requests go out with
`sendto(CardIP:CardPort)` and replies come back through `recvfrom`. SO_SNDTIMEO and SO_RCVTIMEO are
set to the timeout argument, and the select timeout is `timeout*1000` µs. TCP: `connect`, then
SO_SNDTIMEO, SO_RCVTIMEO and SO_LINGER(0). Winsock 1.1 (`WSAStartup(0x101)`). The HAL is opened with
`open(inet_addr(CardIP), CardPort, [+0x112c], AccessType)` (call at 0x10057330).
Send and receive buffers are 0x5A8 = 1448 bytes (CMCHalAPI ctor FUN_10017690).

**Request/retry loop FUN_1001d6d0 (observed).** It holds a global lock (0x100b4ee8) and runs these steps:
1. Drain one stale datagram: `select(0)` + `recv(100)` (FUN_100061f0).
2. Take `tid = counter` and post-increment the counter at CMCHalAPI+0x14e, which starts at 0.
3. Encode. On an encoder error return `1000+err`.
4. Up to **sends = MCFifoTime / MCTimeout** times (+0x13c, 1600/500 = 3; MCMaxSendTime is *not*
   used by the MC HAL): `sendto`. Then up to **MCMaxRecvTime** times (+0x140, = 3):
   `select(timeout = MCTimeout ms)`. A timeout counts as error 10060 (WSAETIMEDOUT). On the very first
   timeout of the first send the loop jumps straight to the next send, so the log shows R/S
   `1/1, 1/2, 2/2, 3/2, 1/3, 2/3, 3/3`, matching `Log/2026-09-16.log` 13:34:41.
5. When data arrives: `recvfrom` and decode (§2). A decoder error 4 (wrong transaction id) means
   "keep listening". Any other decoder error returns `2000+err`.
   If `reply.func == req.func && reply.addr == req.addr` the result is **1 = OK**.
   If `reply.func == req.func + 0x80` the result is **500 + exception code**. Otherwise it is **603**.
6. `Sleep(MCSendInterval)` (+0x144, 1 ms) between sends.
Errors are logged as `MC-<step> ErrCode:<n> Try-Times(R/S):r/s Data:<decoded> Buffer:<raw rx> DataEx:<request>`
(CMCHalAPI::writeMCLog FUN_1001d190).

The timeouts can be changed at run time through HAL slot 7 (FUN_100064e0). fillFifo switches them while
it sends FIFO frames, using `param_1[1099]` (INFERRED to be `FifoTimeout=600`). The log line at 15:03:10
shows 2-second spacing for an output write, so other timeouts are also used.

ipAdd.ini keys that matter for the MC: `MCTimeout=500`, `MCFifoTime=1600`, `MCMaxRecvTime=3`,
`MCSendInterval=1`, `FifoTimeout=600`, `ConnectWait=500`, `MCCore=30` (worker loop period, ms,
INFERRED), `MaxItemPerFrame=60`, `MaxFillItem=2000`, `FifoAlarmNum=30` (the last three are read but
not traced, UNKNOWN use), `MinHardwareVer=20177` (firmware V201.77, compared with RO[1]).

## 2. Frame format (CExtModbus, observed)

The format is "Modbus TCP-like", but the protocol-id field holds a CRC and the function codes are
custom. Header fields are **big-endian**. Payload fields are **little-endian**.

```
off size field
0   2    transaction id, BE (request: counter value before increment; reply echoes it)
2   2    CRC-16/MODBUS of bytes[4:]  — PC→card: big-endian (hi byte first);
                                     card→PC: little-endian in the captured reply.
                                     The PC never checks the reply CRC.
4   2    LEN, BE = number of bytes after this field
6   1    unit id = 0
7   1    function
8   ...  payload (LE)
```

| func | request payload | LEN | reply payload |
|---|---|---|---|
| 0x30 read regs | addr u32, count u16 | 8 | addr u32, count u16, count × u32 (decoder checks `count*4 == len-14`) |
| 0x40 write regs | addr u32, count u16, count × u32 | 8+4n | addr u32, count u16 |
| 0x20 / 0x21 bytes | count u16, count × u8 (no address) | 4+n | count u16 + bytes (checks `count == len-10`) — use UNKNOWN |
| 0x26 bytes at | addr u32, count u16, count × u8 | 8+n | addr u32, count u16 — use UNKNOWN; bypasses the HAL filter |
| f\|0x80 exception | — | 3 | 1 byte exception code |

Registers are 32-bit. The CRC is the standard Modbus CRC. The tables at 0x1008a580/0x1008a680 were
checked against a bitwise implementation in the tests.

**Worked example from the log** (13:34:44, `MC-RecvDataErr ErrCode:502`):
`DataEx:30 2710 12` = read 18 registers at 10000. The request would be
`02 be | crc | 00 08 | 00 30 | 10 27 00 00 | 12 00`. The reply `Buffer:02 be d0 45 00 03 00 b0 02` decodes as
tid 0x02be, CRC 0x45d0 (stored LE), LEN 3, unit 0, func 0xb0 = 0x30|0x80, exception 2 (illegal
address, because this card has no on-board follower). The result is 500+2 = **502**, and the decoded
`Data:000000b0 00000002` is `[func, code]`.
Other logged requests: `40 64 01 22b8` = write reg 100 := 8888. `30 3e8 02` = read 1000×2
(checkMCStatus). `30 c350 1a` = read 50000×26 (system RW block). `40 65 04 270f 02 01 01` = write
101 := [9999, 2, 1, 1] (output 1 on).

**HAL address filter (observed, FUN_10022590 read / FUN_10022710 write).** Before sending, CMCHalAPI
drops some requests (func 0x26 bypasses this):
- Writes to 106–4999 (except 150, 151), 5009–5999 or 51001–58999 are **not sent** and still return
  success.
- Reads of 105–999 (except 150/151) return failure (0) with zeros. Reads of 1101–1999, 3001–4999,
  5009–5999 and 51001–58999 return success (1) with zeros and are not sent.
So in this build the live map is roughly 100–105, 150–151, 1000–1100, 2000–3000, 5000–5008,
6000–51000 and ≥59000. Several legacy register writes in the code are therefore dead, for example
homing at reg 309.

## 3. Register map (MC)

| Reg | R/W | Content | Evidence |
|---|---|---|---|
| 100 | W | 8888 = card restart (FUN_10039300, VM slot +0x168; mf131 and after a firmware download, §19). It is **not** part of the connect sequence (O, parity capture; the old log's first frame came from a restart). 9999 = "apply" at the end of the parameter download (INFERRED purpose) | FUN_10039300; FUN_1002e500 end; log 13:34:41; §3a |
| 101 | W | **command mailbox** `[op, params…]` (§4) | FUN_100315f0, FUN_10032b80, … (all pass addr 0x65) |
| 102 | W | **FIFO data** `[frame_seq, item words…]` (§5) | FUN_10043c20 (vector `[0x40,0x66,count,seq,…]`), FUN_10052e90 |
| 103 | W | FIFO control: 1 clear, 2 start, 3 stop | FUN_10051bf0 / FUN_100515e0 / FUN_100518b0 ("false clearFifo/startFifo/stopFifo") |
| 150, 151 | W | allowed by the filter; CNCModule slot +0xf4/+0xf8 write (150, 5555) and (150, 9999) through another object (UNKNOWN) | FUN_1002c480 |
| 1000+i, i<36 | R | status RO block (below) | updateMCStatusRO FUN_100521b0 |
| 1032 | R | one word read right after the download and kept as the FIFO capacity (cfg +0x47f0; 65000 when the read fails). In 16-bit addressing 1032 is RO word 16 = free FIFO bytes, which equals the capacity when idle (I) | FUN_10054e70 (VM +0x248); §3a |
| 1050..1052 | R | 3 words read by VM +0x1a4 FUN_1003d3a0 (CNCModule +0x2cc) at the end of the start-up (MainApp FUN_00596320); the third is added to cfg +0x4448 (U: a counter), the latched axis words k6 come from the cached RO block | FUN_1003d3a0 (0x41a) |
| 2000+10a+k | R | axis RO, 5 axes × 10 | updateMCStatusAxisRO FUN_100523c0 |
| 5000 | R/W | 1 RW word (UNKNOWN) | FUN_10052600 |
| 5001 | W | `[9999, 9, 0xffff, 0]` before outputs are reset (UNKNOWN) | FUN_10031180 |
| 10000..10017 | R | on-board follower status (18) | updateZFStatus FUN_1004f3e0; log exception 2 |
| 10004 | R | follower flag bit 31 (32-bit from two 16-bit halves) | FUN_100394b0, FUN_1004ae90 |
| 12000+ | R | read through VM slot +0x250 `(12000, n+1)` (UNKNOWN) | cmd sites |
| 50000+i, i<26 | R/W | system parameters (RW). The download writes 50008, 50012, 50014+2n, 50026, 50028, 50036, 50040+, 50106, 50108 (semantics UNKNOWN) | FUN_10052a20, FUN_1002e500 |
| 50200+20a+k | R/W | axis parameters, 5 × 20 words, 14 used | FUN_10052800 |
| 59500 | R/W | **hardware challenge (licence gate)**: the original writes 4 random words and reads 59500 back, then shows "非授权硬件". Intentionally not implemented, answered or analysed (§8) | FUN_10039390 / FUN_1003d090; parity capture |
| 59503 | R/W | single word (UNKNOWN) | FUN_10039280/FUN_100392b0 |
| 59501 | R | 4 words (UNKNOWN) | FUN_1003d280 |
| 59510, 59511 | R/W | (UNKNOWN) | FUN_1003d770…FUN_1003dcd0 |
| 59600+slot (slot<32) | R/W | hardware parameter blocks `[0xffffffff, 0xe0, values…]`. Values are ints, doubles via ftol, or bytes, chosen by a per-field type table | writeHardParam2Card FUN_10050fb0, readParamFromCard FUN_10051320 |
| 60000 | W | **fast-status map**: 60 words, each two u16 register numbers (low half first): 2000, 2002, …, 2098 (the 50 axis-RO words in 16-bit addressing) then 50200+40a+2k for a<5, k<14 (the 14 used words per axis parameter block). The fast length VM+0x1204 := 120 is set before the write | FUN_1003ee50 (VM +0x20c); parity capture (first word 131205072 = 2002<<16 \| 2000) |
| 60001.. | R | "fast" combined status: the registers named in the 60000 map, VM+0x1204 (= 120) words. Words 0–49 → axis RO (VM+0x120, same place as the 2000×50 read), 50–119 → axis RW, 14 per axis (VM+0x1ec, same as the 50200×100 read); the unpacker also knows 120–129 → VM+0x3a0.. and 130–143 → VM+0x56c.., which this map does not fill | updateMCStatusFast FUN_10052c30 |
| 63000 | R | 0x80 words "servo info" (read once at the end of the start-up, written to ServoInfo.txt) | writeServoInfo FUN_10054a00 (VM +0x210) |

**RO block (reg 1000+i)**:
| i | meaning | evidence |
|---|---|---|
| 0 | config word. Bits 17–18 = card variant when the version is <6000 | checkMCStatus FUN_1004ed60 |
| 1 | hardware/firmware version code. `v%10000 >= 6000` → card type `v/10000` | FUN_1004ed60 |
| 4 | inputs 1–12 (bitmask) | FUN_1002b820 (CNCModule +0x150) |
| 5 | outputs 1–10 state | FUN_1002b760, FUN_100390f0 |
| 6, 7 | exposed raw (UNKNOWN) | FUN_1002be20/40 |
| 10, 11, 12 | read by MainApp (UNKNOWN) | MainApp getReg(0,…) |
| 15 | sequence number of the last FIFO frame received; the next frame uses +1 | fillFifo FUN_10052e90 (`param_1[0x33]`) |
| 16 | free FIFO space, bytes (a frame is sent only if free ≥ frame_bytes+2000) | FUN_10052e90 (`param_1[0x34]`, "CalcBufferSize") |
| 19 | ==1 → machine state 4 (running a program) | FUN_10039500 |
| 21 | inputs 13–28 (extended IO board, config +0x4e00) | FUN_1002b820 |
| 22 | outputs 11–26 state | FUN_1002b760 |

**Axis RO record k (reg 2000+10a+k)**: k0 status (bits 0–3 alarm code; byte 2 non-zero = busy;
byte 3 mode, where 2 = homing and 3/4/5 = manual motion, INFERRED from FUN_10039500 / FUN_100395d0).
k1 velocity (pulses/s INFERRED; CNCModule FUN_1002bb20 divides by pulses/mm and takes
`sqrt(vx²+vy²)` for path speed). **k2 command position in pulses** (FUN_1002c070, MainApp FUN_00481c70).
k4 alternate position (INFERRED encoder feedback, chosen by a VM flag). k6 latched position (INFERRED).
The machine state from FUN_10039500 is 4 when RO[19]==1, 1 when an axis has mode 2 and is busy, and 2 when an
axis has mode 3–5 and is busy. Otherwise it is 0.

## 3a. Start-up sequence and status polling (O)

Captured from the ORIGINAL MainApp by the parity harness (`run_app.sh startup`, rebuild simulator
as the card) and traced in NCModule. Rebuild: `MotionController.connect()`, `.startup()`,
`.poll()`, `mlaser/controller/startup.py`; checked request by request (values included) by
`parity/compare/cmp_app_frames.py`.

1. **Connect** (VM event 0, FUN_100571f0 case 0): open the HAL, checkMCStatus FUN_1004ed60
   (`read 1000×2`: config word, version), then, if the card answered, `101 := [9999, 5, 0, 0]`.
2. **Status poll** FUN_1004eab0 (VM +0x144, every ~200 ms while connected). Without a fast map
   (VM+0x1204 = 0): `1000×36`, `2000×50`, `50200×100`, `50000×26`. With one: `1000×36`,
   `50000×26`, `60001×120` (FUN_100521b0, FUN_10052a20, FUN_10052c30).
3. *(licence gate, not implemented: `59500 := 4 random words`, `read 59500`, twice; §8)*
4. **Parameter download** FUN_1002e500 (CNCModule +0x194, from MainApp FUN_004ae4b0 / CNCModule
   +0x48 = FUN_1002b3f0), standard card: `5001 := [9999, 9, 0xffff, 0]`;
   `50026 := ((DI.ServoAlarmType<<21 | DI.ServoAlarm | 0x100000)<<8) | DI.EStop`;
   `50028 := 0` or `[slave<<8 | Y axis, MAC.AllowableError, MAC.AlarmTime]` (dual drive);
   `50036 := MAC.CloseBreakTime`; `101 := [9999, 5, 0, 0]`; per axis a<5
   `50200+40a := 14 words` = [switch-type word FUN_1002b190, soft limits ftol(−0.5·ppm) and
   ftol(SoftLimitMaxLen·ppm) (swapped/negated when GoOriginalDirection ≠ 0), ftol(Acceleration),
   ftol(cfg+0xbc88), ftol(ppm·FastSpeed), ftol(ppm·SecondSpeed), ftol(ppm·ReturnLength), 0,
   ftol(ppm·SpeedRatio), WritePluse, WritePluse, Orig<<16 | Fwd<<8 | Neg input ports, BrakeOutput];
   `50022 := [ftol(LimitDeccFactor·MaxAcc), ftol(LimitDeccFactor·MaxAcc·10)]`; DA reset
   `[9999, 4, 0, 0]`, `[9999, 4, 1, 0]` (setDA FUN_1004d640: ftol(%·k·100), k = 1/0.5/0.4 by DA type);
   outputs `[9999, 2, 0xffff, standby|remote-key bits]`; `50012 := ZF type`;
   `50040..50050 := DI smoothing pairs hi<<16 | lo`; `[118, 5, 0/1]` (only with a follower);
   `50106 := LGP.DOLaser if MP.PWMOutputSync else 0` (bus cards: 50106/50108 by CO2 control
   type); `50008 := (AF.AFType == 1)`; `100 := 9999`; then VM +0x248 (FUN_10054e70):
   `103 := 1` and `read 1032` (FIFO capacity). With the shipped install (ppm 1000, all defaults)
   every value matches the capture, e.g. axis block
   `[131088, -500, 1000000, 20000, 200000, 30000, 20000, 10000, 0, 100000, 10000, 10000, 0, 0]`.
   Not reproduced: bus-card, rotary-axis, AF-focus-axis and precision-compensation branches.
   `cfg+0xbc88` (axis word 4, 200000) and ppm (+0xbc28, 1000) are restored from the card
   (FUN_10039f20), not file parameters.
5. **Fast-status map** FUN_1003ee50 (VM +0x20c): `60000 := 60 words` (§3); from now on the poll
   uses `60001×120`.
6. `103 := 1` (CNCModule +0x220 = FUN_1002bde0), then **servo info** `read 63000×128`
   (VM +0x210). *(licence gate again: 59500 write/read.)*

Before the simulator accepted the 60000 map, the failed write marked the card as disconnected:
the original stopped polling, did not send the second `103 := 1`, and answered "home?" with no
traffic. With the map accepted it keeps polling in fast mode; after the connection it switches
the red light off and its Go Origin dialog sends `101 := [2, 3, 0]` (panel thread) while the
start-up routine ends with `read 1050×3` (CNCModule +0x2cc, §12) — the two coincide in time
(capture of 2026-10-18).

## 4. Mailbox commands (write reg 101)

The first word is an opcode. The same opcodes (with a header word) appear as FIFO items (§5).

| Command words | Meaning | Evidence |
|---|---|---|
| `[9999, 2, mask16, value16]` | set outputs 1–16 (mask/value) | FUN_100304a0 (all, mask 0xffff), FUN_10030620 (single bit); log 15:03:10 |
| `[9999, 13, mask16, value16]` | outputs 11–26 (extended board) | same functions |
| `[9999, 4, ch, value]` | analog DA output, ch 0/1, value 50..10000 (DAMinVal=50; INFERRED 0.01 % of full scale). Used for laser power | FUN_100307d0, FUN_100308f0, setDA FUN_1004d640 |
| `[9999, 3, freq, duty]` | PWM (config +0x48bc / +0x4818; units UNKNOWN) | FUN_100309f0, setPWM FUN_1004d960 |
| `[9999, 17, freq, duty]` | PWM on the bus-card variant (config +0x4728==1, +0x49c8==2) | same |
| `[9999, 1, axis_mask \| 0x66800000 (or 0x78800000), 0]` | axis/servo configuration (INFERRED) | FUN_10051e70 |
| `[9999, 5, 0, 0]` | **alarm reset** (O: CNCModule +0x1e4 = FUN_100315f0, called by Clear Alarm FUN_004b8690, the servo-alarm handler FUN_004afb90 and before the axis-parameter download); also sent at VM start | §13 |
| `[9999, 8, mask]` | zero/reset coordinates of the masked axes (manual origin: mask X\|Y = 3, FUN_004b0060(cfg +0xbbd0)); the PC then clears per-axis offsets and rewrites the soft limits (§17) | FUN_100314b0 (CNCModule +0x1a8) |
| `[3, axis(\|0x80000000), speed, acc, acc*10, distance]` | **jog** a single axis, "start jog". Word 1 is the axis **number** (X 0, Y 1), not a mask: FUN_10032b80 copies its argument, MainApp's jog FUN_005bdae0 passes the panel axis index (O, capture `[3, 0, …]` for X, `[3, 1, …]` for Y). speed = ftol(v·ppm) (capped by FUN_10032b80 at an ftol(·ppm) bound, operand U), **acc unscaled**, distance = ftol(d·ppm) relative. Bit 31 from a bool argument (U; 0 for the XY jog). Values: §11a | FUN_10032b80 (CNCModule +0x4c) |
| `[5, axes_mask(\|flag), speed, acc, acc*10, p1, p2, p4, p3]` | multi-axis **relative** move ("start jog"); every MainApp caller passes target − current (FUN_005bea00, FUN_005ca160; O). The mask is all configured axes (cfg +0xbbd0/+0xbbd4) | FUN_10032910 (CNCModule +0x50) |
| `[117, ftol(ZFFollowSpeed·10), FollowH·1000, UpH·1000, Sensitivity, StepH·1000, 0, ftol(OutSpeed·10), 1]` | follower edge-seek mode (§16) | FUN_10031cc0 (CNCModule +0x110) |
| `101` (single word) | follower cancel; also the first word of 停止 when no job runs (OnStopBtn FUN_005ba0d0, §14) | CNCModule +0xe8 FUN_1002c460 |
| `[1, 0x1f]` | INFERRED stop/disable axes mask | FUN_100301a0 (CNCModule +0x64) |
| `[1, 0x1f, 2, decel, 200000]` | **decelerated stop of all axes** (O layout; decel = FUN_10038de0 = ftol(clamp(MP.JogStopDccFactor·MC.XFastMoveAcc, 2000, FCP.MaxAcc)), **not** scaled by ppm: capture 4000 with the defaults). Replaced by 103 := 3 when VM+0xdc == 1 | FUN_10054900 |
| `[2, axis_mask, 0]` | **Go Origin** (O: `push 0x65` at 0x10032f32); bit n = card axis n, bit 16 = follower | FUN_10032da0 (CNCModule +0x5c), §12 |
| `[103, ZFUpSpeed·10, ZFDockHeight·1000]` then `[118, 5, 0]` | stop path: lift Z to the dock height (only when the ZF state is 0x75), then follower stop. `[118, 5, 0]` only when VM +0x38 is set (a program ran: FUN_10055840, event 9) and SP.m_iEnableLaserType = 0, and VM +0x70 (FUN_100390c0) sends it only with a follower (ZF.ZFType ≠ 0). Earlier read as "stop with deceleration"; corrected from the asm at 0x100575fc (§10.1) | FUN_100571f0 case 10/11 |
| `[118, 4, on, value]` | follower (ZF) on/off through the MC (INFERRED) | FUN_10041740 |
| `[118, 7, …]` | "ZF Signal correction" | FUN_10041630 |
| `[101]` | sent alone in the VM start path (UNKNOWN) | FUN_100571f0 |

**Jog and stop jog (O, D-21..D-23).** Panel jog button pressed: `[3, axis, …]` above. Released
(WM_LBUTTONUP in FUN_005ad9f0, not in step mode) → CManuPanel::jogStop FUN_005cd960 → CNCModule
+0x58 "send stop manu cmd" (event 10). With no program running the VM thread does "vm stop jog":
FUN_10054900 `[1, 0x1f, 2, decel, 200000]`, the optional lift / `[118, 5, 0]` above, state 0x19;
once the axes are idle FUN_10058030 writes **103 := 3** (FUN_100518b0) and **103 := 1**
(FUN_10051bf0). Capture: `[1, 31, 2, 4000, 200000]`, 103 := 3, 103 := 1. In step mode
(MC.IsStepMove) the release sends nothing.

Homing (§12) is `[2, mask, 0]`. The older "z signal go origin" (FUN_10037c10, CNCModule +0x60)
writes `[flag, speed, acc, acc*10, a, b, c]` to reg 309, which the HAL filter drops; MainApp does not
use it for Go Origin. Pause/stop: §14.

## 5. FIFO streaming

**Frame** (FUN_10043c20, observed): `write(102, [seq, item, item, …])`. The builder starts a vector
`[0x40, 0x66, 0, 0]` and appends the FIFO items of one ManuItem at a time. **After each ManuItem's
whole expansion** (a leading 3001 included) it tests `0x4af < words*4`, and closes the frame when the
test is true or at the last ManuItem of the call. A frame therefore never splits one ManuItem's items
and can exceed 1199 bytes by one expansion (306 words = 1224 bytes seen, parity JN-08). It then sets
`count = words-3` and queues the frame on a deque (VM+0x11ec). Rebuild: `frames.build_fifo_frames`
takes groups (one list of FIFO items per ManuItem), and `Job.frames` passes `Job.groups()`.

**Fill** (fillFifo FUN_10052e90, observed): at most 20 frames per call. For each frame it sets
`seq = RO[15]+1+k`, requires `RO[16] ≥ frame_bytes + 2000`, switches the HAL timeouts, and writes. On
failure it runs updateMCStatusRO and retries once after 50 ms ("second/Three fillFifo error").
After each frame it calls updateMCStatusFast. Start, stop and clear go through reg 103.

**Item** = header word `(param_bytes << 16) | opcode`, then the parameters (u32). Opcodes found
(header constants in FUN_10043c20 and helpers FUN_1003ec20, FUN_1003f3e0, FUN_1003f470, FUN_1003f830,
FUN_1003f990):

| header | op | params | meaning |
|---|---|---|---|
| 0x80bb8 | 3000 | 2 | **interpolation step** (below) |
| 0xbb9 | 3001 | 0 | marker: ManuItem class 0xe, and once when the item class changes from 0 (motion) to a process class 1..19 (VM+0x117c = previous class, set after every ManuItem; −1 at program start, so the first process item of a job has none). I: "end of motion block" (§10) |
| 0x40bba | 3002 | 1 | `[ManuItem+0x18]` for ManuItem class 3; MainApp never creates class 3 (U) |
| 0x?270f | 9999 | sub, … | system sub-command (§4, §10.2): `0x10270f [3, freq, duty, on]` laser PWM, `0xc270f [4, ch, v]` DA, `[2/13, mask, val]` outputs, `0x8270f [18, 1000/0]` laser sync |
| 0x807d1 | 2001 | 2 | wait `[condition, timeout_ms]` (FUN_1003ec20): time / input / axis / follower state (§10.1) |
| 0x1807d4 / 0x1407d4 | 2004 | 6 / 5 | card register op: `[0, reg, nbytes, words…]` write, `[1, reg, 4, value, 0x80000002]` wait (AF type 1) (§10.1) |
| 0x80067 | 103 | 2 | Z to absolute height `[speed·10, z_µm]` (§10.1) |
| 0x80068 / 0x200068 | 104 | 2 / 8 | follower on to cut height `[speed·10, vib<<16 \| h·ppm]` (§10.1) |
| 0x100069 | 105 | 4 | follow to pierce height `[speed·10, vib<<16 \| h_µm, up·10, rest_µm]` |
| 0x18006a | 106 | 6 | frog-jump lift (layout only) |
| 0x10006c | 108 | 4 | gradual height change `[speed·10, Δ_µm, target_µm, 1]` |
| 0x8006d | 109 | 2 | relative Z move `[speed·10, Δ_µm]`, + = down |
| 0xc0076 | 118 | 3 | follower control; `[4, min_h, up_h]` crash protect |
| 0x140003 | 3 | 5 | axis move `[axis\|0x80000000, v·ppm, acc, acc·10, pos·ppm]` (focus axis, AF types 5/6) |

**Step item, op 3000** (non-bus card, observed). The source is a 28-byte "ManuItem":
`+0 i32 dx, +4 i32 dy, +8 u16 v8, +0xc i32, +0x10 u16, +0x12 u8 duty, +0x14 u16 freq, +0x16 u8 type,
+0x18 i32 flags (low 4 bits = process layer)`. This field order is confirmed by the `_tempManuItem.olpf`
dump in FUN_100352b0.

```
w1 = (freq & 0xff) << 24 | duty << 16 | (dy & 0xff) << 8 | (dx & 0xff)   ; dx,dy signed int8 pulses
w2 = (duty ? v8 : 0) << 16 | (freq >> 8)                                 ; freq = 5000 if 0
```
dx/dy are per-step pulse increments for X and Y. The PC tracks the direction of each axis and emits the
signed byte. **The item has no duration field: each step is one servo period** (1 ms default).
This was settled in the integration review from MainApp FUN_00462630, which builds the ManuItems.
It sets `+0x14` from a per-sample u16 array, and its slow-start/slow-end overrides write the
configured *frequency* (override block `+0x20`) there. `+0x12` is a duty byte: it is scaled by a
percentage and divided by 100, and zeroed for laser-off samples. The "0 → 5000" default matches the
`GP.CutFreq` default of 5000 Hz. An earlier draft of this doc read `+0x14` as a duration in 0.1 µs
ticks; that reading is withdrawn. `+0x08` (v8) is set to **0xFFFF for every sample** in FUN_00462630
(0x0046297b), micro-joint gap samples of normal cut elements included; only elements of CAD contour
type 4/5 (U) get 0 in their gaps. So w2's high half is 0xFFFF whenever the duty is non-zero (O,
parity JB-02). Its meaning on the card (a power/DA word) is INFERRED. Consequence: one item moves at most 127 pulses per period. With the
real scaling (§11: step pulses/mm = ppr/lead, default 10000/100 = 100; period = InterpolationCycle,
default 1000 µs) that is **1270 mm/s** (5080 mm/s at 250 µs); `Machine.max_step_speed` reports it. On the bus card
(+0x4728==1, +0x49c8==3) w1 instead carries a 14-bit speed and a 2-bit ZF mode in the high half.
That variant is documented here only.

The PC does the interpolation: `MotionCtrl.dll` exports `segInterp`, `arcInterp`,
`segInterp_time` and `newVelocityPlanning`. The card only replays steps.

## 6. Units

- See §11 for the full scaling (step pulses vs position counts).
- **Positions are integer counts.** mm = counts / `pulses_per_mm`. The value is an int in the
  machine parameters (config +0xbc28): `FUN_1002c070` does `AxisRO[a*10+2] / cfg[0xbc28]`, and
  `FUN_1002bb20` does the same for velocity. `File/softPara.ini` `[SC2000] XAxis=-335447 YAxis=-208432`
  are raw pulses written by MainApp FUN_00481c70 from `getReg(3, axis*10+2)` at exit.
  The default pulses/mm is not in the shipped files. 1000 (1 µm/pulse) is assumed by
  `MotionController(pulses_per_mm=1000)` (INFERRED; -335.447 mm is plausible for that log).
- Axis index mapping X/Y/Z/W/... comes from config +0xbbd4.. (0-based card axis numbers).
- Jog/move speed and acceleration are raw ints computed by ftol from doubles (UNKNOWN scale;
  INFERRED pulses/s and pulses/s²). The speed is clamped to a configured maximum.
- DA 50..10000. PWM frequency/duty raw ints (UNKNOWN units).

## 7. Peripherals (summary)

- **Follower ZF**. On-board: MC registers 10000+ (16-bit halves combined `hi<<16|lo`), mailbox op 118.
  Network: `CStdModbus` frames `00 00 00 00 | len=2*vector_size | 00 | func | addr BE16 | count BE16 [| bytecount | BE16…]`
  (FUN_10028740). The tid and protocol id are 0. For func 0x10 the length is one short of real
  Modbus/TCP (observed quirk). Register meanings are UNKNOWN. Version checks: `OnBZFMinHardwareVer=311`,
  `NCZFMinHardwareVer=325`, `MaxZFDownItem=512`. The network codec has no users in this build.
  The live follower is the on-card one: status 10000×18, properties 11000, commit 150 := 9999,
  calibration curve count at 12000 with pairs from 12002, AF heads behind the card at 13000/13200.
  FUN_100394b0 tests bit 15 of register 10004, not bit 31. Full detail is in [devices.md](devices.md).
- **Laser source**: `CRaycusModbus`/`CIPGModbus` copy bytes verbatim. The command strings are built at
  FUN_10027110…100274a0: IPG `EMON`/`EMOFF`/`ABN`/`ABF`/…, Raycus `ESC O`/`ESC S`/`ESC C P` ([devices.md](devices.md)).
  Laser power while cutting is set through the MC DA/PWM commands. The laser on/off gate is the step
  item's laser byte.
- **Gas**: no dedicated protocol. Valves are ordinary outputs and the proportional valve a DA
  channel, both from HardPara (MGP.* ports, §10.3). +0x224 (DO.WaitSignal) and +0x2c4
  (LGP.DORemoteStart) are switched on in FUN_10031180.
- **AF / EC**: Modbus RTU over UDP (slave 1 / slave 2), implemented in `mlaser/devices/` ([devices.md](devices.md)).
- **Firmware update**: see §19 (func 0x26 chunks of 1400 bytes at the file offset, then
  100 := 8888). Implemented in `mlaser/controller/upgrade.py`.
- **IPSet.exe** only configures the PC NIC (`netsh interface ip set address … 10.1.1.x mask
  255.255.255.0 gateway 10.1.1.1`). It does not talk to the card.
- **ControlModule.dll** is a UI widget library (BCG). No device I/O.

## 8. Intentionally omitted

These are not reimplemented, as the brief requires: the **hardware challenge at register 59500**
(random words written and read back at start-up, FUN_10039390 / FUN_1003d090, "非授权硬件"; not
implemented, answered or analysed; the rebuild's start-up skips it and its simulator only stores
the words), HID dongle / `CDog` (FUN_10001000), "Check code"
/ admin-data activation (`Log/Code.txt`), the user-lock checks (`User Lock`, CVirtualMachine slot +0x258, FUN_10042a90),
`CheckUserID`, and the cloud monitor (`CMonitorHalAPI`, HTTP JSON
`updateStatusByDevId/upsertWarnByDevId/updateBasicInfoByDevId` to `MonitorIP:MonitorPort`).

**Working assumption (agreed with the user, not yet verified on hardware):** the 59500 exchange is
a PC-side check. The software authenticates the card, and the only effect seen in the original is
MainApp disabling Start/Frame/Dry Cut (CManuPanel+0x1e8). The card is assumed to accept jobs
without it, so the rebuild has no lockout logic of any kind. To confirm on the first hardware
session (laser physically disabled, e-stop in reach): jog, then Frame, then Dry Cut a small drawing.
If the card ignores FIFO start or raises an alarm without the exchange, the assumption is wrong.
The challenge-response will still not be reimplemented, because that would mean reproducing the
vendor's authentication secret.

## 9. Open questions

1. Scaling resolved in §11; the card's real ppm (sys word 17) is U. Meaning of ManuItem `+0x8`, `+0xc` and `+0x10`.
2. Process items: decoded in §10. Still open: op 106 field meaning, op 3002, 2001 timeout
   behaviour, follower state numbers, the 8-parameter op 104.
3. Resolved in §12 (homing = `[2, mask, 0]`).
4. Meaning of RO words 2,3 and 6–14, 17, 18, 20 and 23–35, the axis-RO words 3, 5, 7–9, and the system
   and axis RW parameter blocks.
5. Network ZF, laser, AF and EC register maps. Whether the card's reply CRC byte order (LE) is
   systematic.
6. Who builds ManuItems. They are built outside the functions analysed here (the VM `+0x44` entry
   FUN_10047c50 receives them), possibly MotionCtrl via CADModule.

## 10. Process items

Tags: **O** observed (address given), **I** inferred, **U** unknown. NC addresses are NCModule.dll,
`0x004…`/`0x006…` are MainApp.exe, `0x100f…`/`0x101…` are CADModule.dll.
Implementation: `mlaser/controller/frames.py` (encoders), `mlaser/controller/process.py`
(MainApp sequences + NC encoder), `mlaser/nc/compiler.py` (emission).
Tests: `tests/test_process_frames.py`, `tests/test_process_sequence.py`, `tests/test_process_compile.py`.

**Parameter offsets (O).** NCModule's config object (`VM+8`) and MainApp's parameter block have
the same layout. The struct offsets in `mlaser/formats/data/mlaser_params.json` therefore name every
field the builder reads. Both use the layer block `+0x4ed8 + 0x480·layer` (FUN_10038da0 = MainApp
FUN_00458c60; CO2 layers start at +0x8058), and pierce stage *s* sits at `layer + 0x158 + 0x58·s`.
Examples: +0x7c GP.CutPower (duty), +0x80 GP.CutFreq, +0x84 GP.CutPeakCurrent, +0x50 GP.CutHeight,
+0x60 GP.CutFocusPos, +0x68 GP.UpHeight, +0x38 GP.ManuType, +0x3d8/+0x3dc/+0x3ec ZF vibration
abatement level thin/thick/type, +0x380/+0x3a8 the slow start/end blocks (GP.UD_Up*/UD_Down*),
+0x410.. smooth pierce. Ini values: `VM+0x860` = FollowOvertime, `+0x864` = SectionDrillOvertime,
`+0x868` = GradualDrillAddOvertime and `+0x1190` = DAMinVal (ini reader 0x10059209..0x10059296, O).

### 10.1 FIFO opcodes

| op | params | encoding (O) | meaning |
|---|---|---|---|
| 2001 | `[cond, timeout_ms]` | FUN_1003ec20(vec, mode, a, b, c, timeout): mode 0 `cond=a`; 1 `(b\|0x10000)<<8\|c`; 2 `(b\|0x20000)<<8\|c`; 3 `c\|0x3000000` | I: 0 = delay *a* ms (class-1 delay pushes timeout 3000, 0x10044294), 1 = input *b* (0-based) == *c* (AFDA.ExeDonePort), 2 = axis *b* state *c* (focus axis), 3 = follower state *c* |
| 103 | `[v·10, z·1000]` | class 0x11: `[ZFFollowSpeed·10, GP.AdvFixHeightCutPos·1000]` (0x10045479); stop path `[ZFUpSpeed·10, ZFDockHeight·1000]` (0x100575fc) | I: Z to an absolute height (µm) |
| 104 | `[v·10, word]` | class 7: `word = vib<<16 \| ftol(max(CutHeight,0.5)·cfg[0xbc28])` (0x10044e41..0x10044fa8); class 0xa: word = 1000 | I: follower on, tracking the cut height (in Z pulses) |
| 104 (8) | `[v·10, word, ftol(ppm·h·MP.PreLaserOnFactor), 1, GRP.CutOffOutEdgeCheckTime, CheckTol·1000, HeadUpHWorkDone·1000, n·10]` | class 7 with flag 0x20 (0x10044ebc) | U (unlimited roll-sheet cut-off edge check) |
| 105 | `[ZFFollowSpeed·10, vib<<16 \| h_µm, ZFUpSpeed·10, rest_µm]` | class 4, first stage: `h = stageHeight` if ≤ MP.DirectDrillMaxHeight else 1.0, `rest = stageHeight − h` (0x1004462a..0x10044753); smooth pierce: h capped at 10 → 1.0 (0x10044475) | I: follow down to h, then rise by rest |
| 106 | `[M+0, M+4·ppm, M+8, 0, u16 M+0x10, vib<<16\|u16 M+0x14]` | class 8 with flag 0x10 and ManuType ≠ 5 (0x100450cd); MainApp FUN_004657e0 fills M from the jump profile (ZFFollowSpeed·10, next height) | I: frog-jump lift during the rapid. **Not generated** |
| 108 | `[v·10, Δ·1000, target·1000, 1]` | class 5, stage 0 → cut height, and smooth pierce (0x10044a05, 0x10044c0c) | I: gradual height change while the beam is on |
| 109 | `[v·10, Δ·1000]` | class 4 later stages `Δ = −(h − h_prev)·1000` at ZFUpSpeed (0x10044763); class 5 to the next stage; class 8 `−GP.UpHeight·1000` at **ZFFollowSpeed** (0x1004514e); class 9 `−ZFDockHeight·1000` at ZFUpSpeed (0x10045216); class 0xa `(−CutHeight − 1)·1000` | I: relative Z move, positive = down |
| 118/4 | `[4, min_h, ftol(MP.ManuCrashProtectUpHeight)]` | FUN_1003f3e0, only with MP.EnableManuCrashProtect and a follower; `min_h` = 0 before follow/pierce, `ftol(MP.EnableManuCrashProtectMinHeight)` after a lift | I: crash protection off / on |
| 3 | `[axis\|0x80000000, ftol(ppm·v), acc, acc·10, ftol(ppm·pos)]` | FUN_1003f470 AF types 5/6 (axis 0x10 for type 5 (0x1003f724), AF.AFAxis +0x4ad8 for type 6; acc at cfg+0xbc80+0xa0·axis), v clamped to ≥ 1; only when the target ≠ VM+0x11d0 (the focus position read from the card at program start); returns the wait `ftol(\|Δpos\|/v·1000 + 50)`; then FUN_1003f830 waits mode 2 on the same axis with timeout VM+0x860 + that time | I: focus axis absolute move |
| 2004 | `[0, 0x65, 0xc, 3, x, y]` / `[1, 0x3f2, 4, ms+10000, 0x80000002]` | AF type 1 (FUN_1003f470 / FUN_1003f830, after a `[50, 3000]` delay) | I: write mailbox 101 on the card / wait until reg 1010 matches. x, y U |
| 3001 | — | see §5 | I |
| 9999/3 | `[3, freq, duty, duty≠0]` | class 1 sub 1/2 (0x10044109; 17 on the bus card) | laser PWM. Freq is sent raw from ManuItem +0x14 (no 5000 default here) |
| 9999/4 | `[4, ch, value]` | class 1 sub 3 (gas DA), FUN_1003f990 (peak current) | DA, 10000 = full scale |
| 9999/2, /13 | `[2, 1<<p, v<<p]` / `[13, 1<<(p−10), v<<(p−10)]` | class 1 sub 4, 0-based port p 0..9 / 10..25 (0x100441cd) | outputs |
| 9999/18 | `[18, 1000]` / `[18, 0]` | class 1 sub 8, `+4 == 1` → 1000 (fiber/step card), 1001 (CO2 bus), CO2DOLaser port 9/10 (0x100442d4) | I: laser sync with the step items. The job loop's sync on never reaches the card (cleared by the start cut, §10.3); the sync off follows every cut block |

Follower states waited for (mode 3, I): **4** after op 105, **3** after op 104, **2** after
ops 103/108/109. The timeout is FollowOvertime for start/end cut and SectionDrillOvertime for the
pierce (plus the stage time for gradual stages).

Peak current DA (FUN_1003f990, O): only when LGP.LaserControlType == 3 (IO), LGP.LaserDAPort ≥ 1, a U
flag at cfg+0x4a51, not in a dry run, and the value changed (VM+0x11c8, zeroed at program start by
FUN_10047c50, so a first peak of 0 % sends nothing). The value is
`ftol(pct · (0.95 if LGP.LaserType == 7) · {1, 0.5, 0.4}[LGP.LaserDAType] · 100)`, raised to DAMinVal
when it is between 0 and DAMinVal. It is sent to channel `LaserDAPort − 1`.

### 10.2 ManuItem classes (FUN_10043c20)

| class | made by (MainApp) | NC output |
|---|---|---|
| 1 sub 1 | laser on/off FUN_00464ab0 / FUN_00464c40 | 9999/3 |
| 1 sub 3 | gas DA | 9999/4 `[+4, +0x18]` |
| 1 sub 4 | outputs (gas valves, LGP.DOLaser) | 9999/2 or /13 |
| 1 sub 5 | FUN_00464d60 (delays) | 2001 mode 0 `[ms, 3000]` when ms > 0 |
| 1 sub 6 | — | 118/4 |
| 1 sub 8 | FUN_004590a0 | 9999/18 |
| 3 | not made | 3002 |
| 4 | pierce stage start (flags `s<<4 \| layer \| first<<8`; smooth `0xe0`) | crash off, peak DA, focus, 105 (first) or 109, ZF wait |
| 5 | pierce stage end when gradual | peak DA, (gradual) focus, 108 (s = 0) or 109 at `clamp(\|Δh\|/t, 0.1, 200)` mm/s, ZF wait unless bolt drill |
| 6 | clean residue (`*p == 3`) | focus wait + ZF wait (also without a follower) |
| 7 | start cut, follow mode | crash off, focus, peak DA, 104, wait 3 |
| 8 / 9 | end cut lift / final lift | 109 + wait 2 (+ crash on for 8); 106 for the frog jump |
| 0xa | start cut, GP.ManuType 1 (fix height) | 104 `[v, 1000]`, wait 3, 109, wait 2 |
| 0xb | start cut, GP.NoFollow | peak DA only |
| 0xc, 0xd, 0xf, 0x10 | — | nothing (skipped before the class chain) |
| 0xe | marker | 3001 |
| 0x11 | start cut, ManuType 5 (adv. fix height) | 103, wait 2 |
| 0x12 | before a rapid that leads to a cut (0x0045b331) | focus pre-position for the next pierce/cut (+ peak DA); also without a follower |
| 0x13 | after a gradual + bolt-drill stage | wait 2 (only with a follower), focus wait |
| 0 | samples (FUN_00462630), bolt-drill dwell (FUN_00464d60 motion branch) | 3000 |
| 2, 0x14..0xff | — | 3000 from the item's fields (final else-branch) |

The follower classes 4, 5, 7, 8, 9, 0xa, 0xb and 0x11 test `cfg+0x4994 != 0` (ZF.ZFType). With
ZFType 0 they fail it and fall into the final else-branch, so each becomes **one op-3000 step from
its own fields** (dx +0, dy +4, freq +0x14, duty +0x12, +8): for the rebuild's items (all zero) a
zero-delta beam-off period. Class 6 (+0 == 3) still emits the focus wait and the follower wait
(parity JN-03; the earlier "dropped" was wrong). MainApp itself does not create them without a
follower: the pierce returns early on ZFType 0 (FUN_00464e10), and the start/end cut follow and
lift items need the following state, so **without a follower there is no pierce and no lift**.
MainApp also skips the pierce when GP.ManuType ∉ {2, 3, 4, 6, 7} and smooth pierce is off,
and in a dry run (MainApp+0x4847) unless FC.EnableEmtptMoveFollow.

Start state (program start FUN_10047c50 / ctor FUN_10056850, O): VM+0x117c (previous class) = −1,
VM+0x11c8 (peak) = 0, VM+0x11d0 (focus position) = the card's focus-axis position. Rebuild:
`ManuEncoder(cfg, layers, dry, focus_pos=0.0)`.

### 10.3 MainApp sequences (O)

- **Gas select FUN_004645d0(type, pressure).** Gas types 0..5 are Air, O2 and N2 low pressure,
  then the same three at high pressure. Valve outputs are at `cfg + 0x1c + (type+7)·0x28` = MGP.LowAir 0x134,
  LowO2 0x15c, LowN2 0x184, HighAir 0x1ac, HighO2 0x1d4 and HighN2 0x1fc. The record table starts at
  ZF.DOFollow 0x1c with a stride of 0x28. Proportional index `k = type − 3` when the type is > 2. The switch outputs are
  MGP.Ratio{Air,O2,N2}Switch `cfg + 0x1c + (k+0x16)·0x28`, and the DA channel is `MGP.Ratio{Air,O2,H2}`
  (+0x92c + 4k) − 1. When the type changes, the old valve and switch are closed and the old DA is set to 0. Then
  the new valve and switch are opened, followed by a delay of MGP.iProportionalOpenSleep (+0x4e08) and the DA.
  The DA value is `ftol(10·p/MGP.NewDAMAxPressure{Air,O2,N2}(+0x3578+8k) · 1000)` (FUN_00635fd0 · 1000, 0x004648a3).
  With DO.Gt*EnableGasDAMap and a map string it is a piecewise-linear map instead (not reproduced).
- **Gas delay FUN_004649c0.** The delay is the sum of GC.DirectGasDelay (first gas of the job, ctx+0x163),
  GC.GasDelay (gas currently off, ctx+0x1e0) and GC.ChangeGasDelay (type changed, ctx+0x164), minus the time already
  spent. It is emitted as class 1 sub 5, and the current type is stored.
- **Gas off FUN_004648d0.** The current valve is closed, and when the type is < 3 its proportional DA is set to 0.
  The switch is left as it is (O).
- **Laser on FUN_00464ab0(freq, duty, on).** LGP.DOLaser (+0xbc) is switched on, then class 1 sub 1 is sent with
  `+0x14 = max(freq,1)` and `+0x12 = on ? duty : 0`. **Laser off FUN_00464c40.** DOLaser is switched off and
  sub 1 is sent with duty 0. The ManuItem is reused, so +0x14 keeps the last frequency.
- **Pierce FUN_00464e10.** The stage count comes from GP.ManuType: 2 → 1, 3 → 2, 4 → 3, 6 → 4, 7 → 5 stages.
  The loop runs **s = N−1 … 0** and does the following for each stage:
  gas select(DrillGasType, DrillGasPressure), gas delay, class 4 (first flag on s = N−1), laser on (DrillFreq,
  DrillPower), delay(stage+0x40 "BeforeLaserOffDelay"). Next comes either a plain delay(DrillDelay ms) or class 5
  when GP.EnableGradualDrill (or EnableFocusGradual with AF 1..6) is set and the time is ≥ 1 ms.
  **Bolt drill** (stage+0x48 = GP.BoltDrill_Enable_{s+1}): the non-gradual dwell goes through
  FUN_00464d60's motion branch, i.e. `(1000 / AX.InterpolationCycle) · DrillDelay` zero ManuItems
  (class 0, all fields 0 from FUN_00413760, so the card replays zero-delta periods with the beam
  **off**). Then FUN_00458650 ramps the PWM with `n = (1000 / cycle) · DrillDelay` class-1 sub-1
  items built on a copy of the zero template: item k = 1..n has `duty = trunc(DrillPower + k·dd)`,
  `freq = trunc(DrillFreq + k·df)`, `dd = (BoltDrill_Power − DrillPower)/n` (stage +0x4c),
  `df = (BoltDrill_Freq − DrillFreq)/n` (+0x50), x87 chop mode (0x45871c); with a gradual stage the
  ramp follows class 5 and is followed by class 0x13. The reused ManuItem keeps DrillFreq, so the
  laser off afterwards sends it (O, parity JB-06). The stage then ends with laser off, delay(+0x44
  AfterLaserOffDelay) and gas off. The smooth pierce
  (GP.EnableSmoothPierce +0x410) is cut gas, class 4 `0xe0`, laser on (smooth freq/duty), then class 5 `0xe0`. It
  goes straight into the cut, so there is no laser off. Afterwards comes the clean-residue spiral (GP.CleanResidue_Enable +0x310), not
  reproduced.
- **Start cut FUN_00465460.** The steps are cut gas select (not in a dry run) and the follow item when the head is not
  already following. The follow item is class 0xb for NoFollow with ManuType < 2, 0x11 for ManuType 5, 0xa for 1 and 7 otherwise.
  ctx+0x161 is set for 7/0x11. Then come the gas delay, laser on(CutFreq +0x80, CutPower +0x7c) and delay(GP.LaserOnDelay
  +0x88). For AF types 5/6 the focus move time is subtracted from the gas delay.
- **Cut block.** The job loop FUN_00459100 pushes FUN_004590a0(1) (laser sync on) into the vector
  [ebp-0x194] (0x0045a101) and then calls the start cut on the same vector (0x0045a166, when
  ctx+0x110 < 2), whose first instruction block is `resize(0)` (0x0046546a). The sync on is lost:
  a normal cut has **no** `9999/18 1000` before its samples. FUN_004590a0(0) (sync off) after the
  samples stays (0x0045a96f) (O, parity JB-04). Element type 4 (cut without pierce) has no sync at all.
- **End cut FUN_004657e0.** It sends delay(GP.LaserOffBeforeDelay +0x8c), laser off and delay(LaserOffAfterDelay +0x90). Gas
  off is skipped when the next element is a cut and GP.NoCloseGasInManu (+0x42) of the **current**
  element's layer ([ebp-0xa8], 0x00465be8) is set, or when GP.ShortDistGasKeepOn (+0x3e) of the
  **next** cut's layer ([ebp-0x94], 0x00465c19) is set and the hop is shorter than FC.ShortNoUpMaxLength (+0x4918).
  When the head is following, a lift is sent: class 8, or class 9 when no cut follows. The lift is skipped when
  GP.ShortDistNoUp (+0x3d), (ManuType 0 or GP.PreDrill), !NoFollow, equal cut heights (< 0.01) and a short hop all hold.
  The frog jump (FC.EnableLeapFrogUp +0x4900, next element a rapid, crash protection off)
  sets flag 0x10 and fills the op 106 geometry. The time is checked against JumpAddTime (not reproduced). The short-hop
  lift flags (ShortDistNoUp +0x3d, ManuType, PreDrill +0x40, NoFollow +0x3f) come from the **next** cut's layer, the
  cut-height comparison uses both (0x00465cdf, O, parity JB-05).

### 10.4 Per-sample duty and frequency (O)

- **Power/frequency vs speed** is not done in FUN_00462630. It is done in CADModule (CInterpMrg + 0x610). MainApp
  passes (GP.CutSpeed, GP.PowerAdjustWithSpeed +0x43, GP.CutPower, power nodes, GP.FreqAdjustWithSpeed +0x478,
  GP.CutFreq, freq nodes) to slot 0x220 (0x004598b9 → FUN_100f2970 → FUN_101049c0). The nodes are int pairs
  (speed %, value %). Collinear nodes are dropped (FUN_101033a0), and the segments are
  `[x_i·0.01·v, x_{i+1}·0.01·v]` with value `y_i·0.01·base + (v_s − x0)·slope` (FUN_10104880).
  Per sample (FUN_10103030 duty byte, FUN_10103120 freq u16) the result is `value + 0.5` converted
  with the x87 rounding control set to **chop** (0x101030d0 `or eax,0xc00` before `fistp`), i.e.
  `trunc((v_s − x0)·slope + (y0 + 0.5))`, when `v_s ≤ v − min(0.02·v, 0.1)`, and `base` otherwise
  (parity JC-12; the earlier "ROUND" was this truncation). The GP.PWMCurveNodes **string format is U**.
  `process.parse_curve_nodes` reads integers pairwise.
- **`local_100` in FUN_00462630 is not a speed curve.** It is MC.AdvMicLinkPower_Pre (+0x4df4), used when
  MC.EnableAdvMicoLink (+0x4df1) is set and the element has micro joints (param_12 == 1). The gap
  samples get `duty = duty·pct/100` (integer); `+8` stays 0xFFFF (it is zeroed only for CAD contour
  types 4/5, U). Without it their duty is 0.
- **Micro-joint gap samples** (FUN_00469950, O, parity JB-03): for each gap [start, end] (param_9,
  arc lengths on the run, I) the samples idx(start) .. idx(end) **inclusive** are gap samples,
  idx(p) = the first sample with s ≥ p; a position beyond the last sample is dropped, and an
  unmatched start runs to the last item (0x00462e33). A 1 mm gap at 100 mm/s covers 11 samples.
- **Slow start / slow end** (blocks +0x380 / +0x3a8 = GP.UD_Up* / GP.UD_Down*: +0 enable, +8 length,
  +0x10 speed, +0x18 laser-control enable, +0x1c duty, +0x20 freq). When both enables are set, the samples whose
  arc length from the start is less than the length (or the remaining length, for the end block) get duty +0x1c and freq +0x20
  (0x00462a20..0x00462ba6). The speed (+0x10) is handed to the planner (0x004596e3..0x00459701). The
  rebuild applies it by splitting the run and lowering the feed of that piece (I).
- ManuItem +0x08 is set to 0xFFFF for every sample (it is the w2 power word); +0x0c is a per-sample byte (U).
- **Pulses** (FUN_00473bd0, O, parity JB-01): one ManuItem per sample **including sample 0**
  (delta from the element start = 0, beam on at the curve duty). Each delta is
  `ftol((x_i·1e4 − x_(i−1)·1e4) · ppu + carry[axis])` with ppu = (WritePulse/SpeedRatio)/1e4
  (FUN_00458b60) and `carry = value + carry − delta`; the carry globals 0x9f2358[0..1] are never
  reset. No step to the exact end point is added. docs/nc.md §1.1 has the rebuild mapping.

### 10.5 Rebuild choices (I)

- `Machine()` uses `ProcessConfig.bench()`: ZF type 1, gas valves on outputs 1/2/3, proportional
  valve on DA 1, red light 4, dedust 5, lube 6. The HardPara defaults are all 0, i.e. no follower and no gas outputs.
  `Machine.from_dat_dir(dat, ini_path=…)` loads the real values. `ProcessConfig.output_map()`
  replaces the placeholder `ui_adapter.DEFAULT_OUTPUT_MAP`.
- A layer without pierce stages but with `pierce['time_s']` keeps the phase-1 beam-on dwell
  (zero-motion steps), preceded by gas select and gas delay.
- `Job.duration_s` stays steps × period. `Job.process_time_s` estimates the timed waits and Z moves.
- Not generated: op 106 frog jump, clean residue, the
  8-parameter op 104, AF types 1–4, the CO2/bus-card variants (17, 1001), the gas pressure maps and
  class 1 sub 6 (no MainApp creator was found among the sequences above).

### 10.6 Simulator behaviour (owner B)

Implemented in `simulator.py` (I model): op 2001 mode 0 holds the queue for *ms*; mode 1 waits for an
input, mode 3 for the follower state, with timeout → RO[7] bit 4 (input) / bit 3 (follower) and the
program stops; mode 2 is always met. Ops 103/108/109 → follower state 2, 104 → 3, 105 → 4 (with a
height value); op 3 sets the extra axis position; 9999/3 keeps `[freq, duty, on]`, 9999/18 the laser
sync source. Still not modelled: 118/4 crash protect, Z travel time. Original list:
op 2001 mode 0 (hold the queue for *ms*), modes 1–3 (wait for input, axis or a simulated follower state,
with timeout → alarm). It would also add ops 103/104/105/108/109 (a Z axis / follower height model reaching the
state numbers above), op 3 (an extra axis), 9999/3 (PWM state), 9999/18 (laser gated only while
on) and 118/4 (crash-protect flag).
Note (agent T): the original never sends `9999/18 1000` before a cut (§10.3, parity JB-04), so a
model that gates the beam on the laser sync would never cut; only the sync off is on the wire.


## 11. Machine scaling and speed cap (O unless tagged)

Two different units are in play.

**Step pulses (FIFO op 3000 dx/dy).** MainApp `FUN_00462630` converts each sample with
`pulses = Δ(mm·10000) · f(axis)` (DAT_0084cc18 = 10000.0), the factor from `FUN_00458b60(axis)`:

```
f = WritePluse[axis] / SpeedRatio[axis] / 10000        (cfg +0xbcb0 int / +0xbca8 double, stride 0xa0)
step_ppm = WritePluse / SpeedRatio = pulses per revolution / mm per revolution ("lead")
```
`axis` is the card axis of X / Y (cfg +0xbbd4 / +0xbbd8). For a rotary (tube) axis
(cfg +0x4e68 set and axis == +0x4e70) the lead is `diameter(+0x4e80)·π` or +0x4e90 and the ppr +0x4e74.
The parameters are `MachineAxisConfig_n`: `MAC.WritePluse` ("Encoder resolution / pulses per rev",
default **10000**) and `MAC.SpeedRatio` ("Lead", default **100** mm). Default step scale = **100 pulses/mm**.
The old `A0.PulseEquivalent` (+0xb548) is not referenced by any code.

**Position counts (axis RO k2/k4, velocities, mailbox jog/move/home speeds).** Divided/multiplied by
the int at cfg **+0xbc28** (`FUN_1002c070`, `FUN_1002bb20`, jog `FUN_10032b80` ftol(speed·ppm)). +0xbc28 is
**not a file parameter**: `FUN_10039f20` (VM +0x208, "hardware restore") copies it from the card's
system RW block, and MainApp shows "Hardware restore failed" (0x48a17x) when it is 0. Default UNKNOWN;
`softPara.ini` `XAxis=-335447` suggests 1000 counts/mm (I).

**Servo/interpolation period** = `AX.InterpolationCycle` (cfg +0x4910, µs, default **1000**), also read
back from the card. MainApp uses it as the sample period (`+0xf0 = cycle·0.001` ms, `1000/cycle` samples
per ms at several MainApp sites, e.g. `(1000 / cycle) * ms`). Note: the `CHardwarePropView` constructor (FUN_005f9f70) forces
+0x4910 = **250** when the hardware page is opened (purpose U).

**Card parameter blocks (16-bit addressing).** 32-bit word *i* of the system block is register
`50000 + 2i`, word *k* of axis *a* is `50200 + 40a + 2k` (writes in FUN_1002e500 to 50028/50036/
50200+40a; reads of 26 / 100 consecutive words in FUN_10052a20 / FUN_10052800, VM mirrors +0x304 /
+0x1ec, 14 words per axis). Decode (`FUN_10039f20`, case c → word c+5 for the system block):

| block word | cfg | meaning |
|---|---|---|
| sys 5 (50010) | +0x4910 | interpolation cycle µs |
| sys 6 (50012) | +0x4994 | ZF type (download: 0 or follower index+1) |
| sys 7, 8 | +0x3f30/34 | DA1/DA2 correction |
| sys 14 (50028) | +0xbc14/+0xbc1c/+0xbbd8 | dual drive: slave<<8 \| Y axis (0 = off) |
| sys 15, 16 | +0xbc20/24 | dual-drive tolerance / alarm time |
| **sys 17 (50034)** | **+0xbc28** | **position counts per mm** |
| sys 18 (50036) | +0xbc50 | brake release delay |
| sys 20..25 (50040..) | +0x3ec4.. | 6 pairs hi<<16\|lo (U) |
| ax 0 | +0xbc58 | limit / origin switch types (FUN_1002b190 / FUN_10038d00) |
| ax 1, 2 | +0xbc78 | soft limits, counts |
| ax 3, 4 | +0xbc80/88 | acceleration, acceleration time (raw) |
| ax 5, 6 | +0xbc90/98 | home fast / slow speed, counts/s |
| ax 7 | +0xbca0 | home return distance, counts |
| **ax 9** | +0xbca8 | **lead · ppm** (download: ftol(ppm·lead)) |
| **ax 10** | +0xbcb0 | **pulses per revolution** |
| ax 11 | +0xbcb4 | U |
| ax 12 | +0xbcb8/bc/c0 | negative/positive limit and origin input ports (bytes) |
| ax 13 | +0xbcc4 | brake output |

The download `FUN_1002e500` (CNCModule +0x194) writes `[9999, 9, 0xffff, 0]` to 5001, the dual-drive
words, the brake time, `[9999, 5, 0, 0]` (alarm reset), the five axis blocks, 50022, PWM/DA setup,
50012, 50040.., `[118, 5, 1/0]`, 50106/50108 (PWM sync), 50008, and finally **100 := 9999** ("apply"),
then VM +0x248.

**Formula and defaults:**
```
step_ppm[a]  = ppr[a] / lead[a]                     = 10000 / 100 = 100 pulses/mm (defaults)
v_max        = 127 / step_ppm / period              = 127 / 100 / 1 ms = 1270 mm/s   (250 µs: 5080 mm/s)
position mm  = counts / ppm(+0xbc28, sys word 17)   (card value; 1000 counts/mm INFERRED)
mailbox speed= mm/s · ppm, distance = mm · ppm
```
Rebuild: `client.CardScaling` / `MotionController.read_scaling()`; `UiController` reads it at connect
and (default `adopt_card_scaling=True`) sets `Machine.pulses_per_mm_x/y = step_ppm`, `Machine.period_ms`,
and the client's position divisor = ppm. The simulator exposes the same blocks (`ppm`, `step_ppm`, `ppr`).

## 11a. Manual motion values (O, agent P; parity D-20..D-32)

All values come from parameters; ppm = cfg +0xbc28 (position counts/mm, card sys word 17, int),
ftol = MSVC `__ftol2` (truncation toward zero, FUN_00640740 in MainApp, FUN_10061360 in NC).
Default values in brackets are those of the shipped ParaModule defaults and of the capture.

| Action (MainApp function) | Wire | Values |
|---|---|---|
| jog press, continuous (FUN_005bdae0, asm 0x5bdfb7..0x5be101) | `[3, axis, ftol(ppm·v), acc, 10·acc, ftol(ppm·d)]` | axis = panel index (X 0, Y 1); v = MC.JogFastSpeed (+0x4808, [200]) when MC.IsFastMode (+0x4802) else MC.JogSlowSpeed (+0x4810, [50]); acc = ftol(MC.XFastMoveAcc (+0x4868, [4000])), slow: ftol(acc·0.1) (DAT_0083d520) → [400]; d = ±MAC.SoftLimitMaxLen of the card axis (+0xbc78 + 0xa0·axis, [1000]) + MP.SecondSoftLimitOffsetX/Y when MP.CO2EnableSecondSoftLimit and SP.m_iEnableLaserType = 1; with MS.EnableSoftLimit (+0x4843) clamped to [offset, offset + stroke] (axis offset +0xbcd8) |
| jog press, step mode (MC.IsStepMove +0x47f4) | same | d = ±MC.StepLength (+0x47f8; the panel step box); the release sends nothing |
| jog release (FUN_005cd960 → NC +0x58) | `[1, 0x1f, 2, decel, 200000]`, 103 := 3, 103 := 1 | decel = ftol(clamp(MP.JogStopDccFactor·MC.XFastMoveAcc, 2000, FCP.MaxAcc)) [4000]; §4 |
| Position-to (FUN_005c2790, mp112) | `[5, 3, ftol(ppm·v), ftol(a), …, ftol(ppm·Δx), ftol(ppm·Δy), 0, 0]` | v = JogFast/JogSlowSpeed; a = MC.XFastMoveAcc·0.8 (DAT_00863330) ·0.1 when slow |
| return to mark / tool origin / manual origin / 回零位 (FUN_005c97f0, FUN_005ca160, FUN_005ca730, FUN_005bc220; panel step 9 → FUN_005bea00 → NC +0x50) | `[5, 3, ftol(ppm·v), ftol(MC.XFastMoveAcc), 10·…, ftol(ppm·Δx), ftol(ppm·Δy), 0, 0]` | v = 100 (DAT_008341f0) when the target is (0, 0) within 0.001 (FUN_00473c10), else MC.XFastMoveSpeed (+0x4858, [100]); manual: min(XFastMoveSpeed, 100). Δ = target − cached head position in mm (counts/ppm), so `(0.93 − 1.93)·1000` truncates to −999 (capture) |
| laser off (NC +0x170 FUN_10030f70) | `[9999, 3, f, 0, 0]`, `[9999, 17, f, 0, 0]` | f = LC.PtLaserFreq (+0x48bc, [1234]); plus LGP.DOLaser (fiber, control type 2/3) or LGP.CO2DOLaser + CO2 DA (CO2) off |
| laser on, point shot (NC +0x168 FUN_10035530) | `[9999, 3, f, p, 1]` (twice when MC.PtLaserTime_ms = 0) | needs the gate flag (+0x4a51 fiber / +0x4a52 CO2) and f > 0; p = MC.LaserPointPower (+0x4818, [50]); MC.PtLaserTime_ms > 0: timed program through the FIFO (I in the rebuild: `laser_test`) |
| soft limits (NC +0x198 FUN_100341b0, asm 0x10034230..0x100342ba) | `50202 + 40·axis := [ftol(min(v, −0.5)·ppm), ftol(max(v, 0.5)·ppm)]` for X, Y (+ dual-drive slave) | v = MAC.SoftLimitMaxLen (+ CO2 second offset as above), negated when MAC.GoOriginalDirection ≠ 0 → [−500, 1000000]; rotated (skew, +0x4cd8) limits not reproduced |
| axis switch words (NC +0x2a4 FUN_100316b0) | `50200 + 40·axis := FUN_1002b190(switch block)` for axes 0..4 except the ZF axis (+0x4ab8, 3 in the capture) | flag +0xbc76 := 1 unless (enable ∧ MS.EnableSoftLimit), +0xbc75 := ¬enable (mode 1/2) → 131088 = SampleType bit 4 \| bit 17 |

Rebuild: `UiController.jog_start/jog_step/jog_stop/_vm_stop_jog/move_by/move_to/return_zero/
laser_press/laser_release/laser_off/startup_homing`, `MachineOps.set_mark/return_mark/
write_soft_limits/write_axis_switch_words`, values from `Machine.from_params` (`jog_speed`,
`jog_fast_speed`, `move_speed`, `move_acc`, `jog_stop_factor`, `max_acc`, `pt_laser_freq`) and
`Machine.param(key)` (ParamDef defaults when not loaded). Tests: `tests/test_machine_manual.py`.

## 12. Homing (O)

MainApp sets the homing mode cfg **+0xbba8** and calls CNCModule **+0x5c** (`FUN_10032da0`):
command 0xbbf → mode 1 (X only), 0xbc0 → mode 2 (Y only), Go Origin / start-up homing → mode 3 (XY),
0xbc2 "ZF first then XY" → ZF go-origin (CNCModule +0xec), Sleep(200), mode 3. FUN_10032da0 refuses
unless connected and the machine state is 0/1, builds the mask

| mode | mask |
|---|---|
| 1 | X | 
| 2 | Y |
| 3 | X \| Y |
| 4 | X \| Y \| 0x10000 (follower) |
| 5 | X \| Y \| W (cfg +0x4ad8) |
| 8 | 0x10000 |
| argument ≠ -1 | 1 << argument |

writes **`[2, mask, 0]` to the mailbox (101)**, refreshes the axis RO block and clears the per-axis
"homed" words (+0xbccc). The card performs the sequence itself using the downloaded axis words
(home fast/slow speed, return distance, switch type and input, direction `MAC.GoOriginalDirection`
+0xbc6c, Z-phase/second homing flags); while homing an axis reports mode 2 + busy (machine state 1).
Completion (O, D-20): the axis status word k0 **bit 15 = homed**; NCModule FUN_10039590 (VM slot
0x1f8, CNCModule +0x224) collects it as a mask and MainApp's status timer FUN_00598280 reports
系统回原完成 (mp125) when it covers the homed axes. Panel buttons: 系统回原 (mp29_1, sysGoOrigin
FUN_005b5de0: mf156 prompt, panel step 8 = FUN_005b6070 mode 3, 4 with AF.AFType 5, 5 with 6);
回零位 (mp29, FUN_005bc220) is **not** a go-origin but a move to the coordinate zero (§11a, §17);
Z回原 (mp30, FUN_005bec40) acts only with a follower (ZF.ZFType ≠ 0; otherwise no traffic).
Start-up (FUN_00596320 after the connection): NC +0x164 gate off, NC +0x178 red light off
(`[9999, 2, 8, 0]` with LGP.DORedLight 4), the mp119 prompt when softPara NormalExit = 1, X|Y not
homed and the coordinate system is not Manual → sysGoOrigin; at the end CNCModule +0x2cc
(VM +0x1a4 FUN_1003d3a0) `read 1050×3` (third word accumulated into cfg +0x4448, a counter, U).
Rebuild: `MotionController.home(mask)`, `home_mode(mode)`, `UiController.home("X"|"Y"|"XY"|"XYZ"|"Z")`
(state "origin", `homed` once no axis is homing, no alarm is active and the homed mask from bit
15 covers the requested axes), `UiController.startup_homing(confirm)`, `AxisStatus.homed`,
`MCStatus.homed_mask()`; simulator status word sets bit 15 for homed axes. Simulator: axes run to 0 at
the "fast" speed. "Edge Seek" (mf19, `mp88_*`) is in §16.

## 13. Alarms and status words (O)

MainApp status timer `FUN_00598280` (asm 0x59a265..0x59a5cf):

* Axis status word (axis RO k0): bits 0..5 alarm flags (below), **bit 15 homed** (FUN_10039590),
  bits 16..23 busy, bits 24..31 mode (2 homing, 3 jog, 5 move).
* **RO[6]** (CNCModule +0x228 = getReg(0, 6)) bit *b*: *b* < 24 or *b* = 24 → axis *b* alarm, the
  axis status word (getReg(3, 10b), axis RO k0) bits 0..5 raise codes `8100 + 32b + k`:
  0 hard +limit, 1 hard −limit, 2 soft +limit, 3 soft −limit, 4 servo input alarm, 5 dual-drive alarm
  (`EtherCATAxisErrorInfo_0..5`, "%s …" with the axis name). Other bits → code `8000 + b`:
  25 bus fault, 26 output fault, 29 servo input alarm, **30 emergency stop** (`EtherCATErrorInfo_1_*`).
* **RO[7]** (+0x22c) bits 0..7 → `9000 + b`: illegal command, interpolation data length, axis command
  failed, FTC command failed, PLC command failed, **5 FIFO starvation**, immediate command error,
  **7 overspeed** (`EtherCATErrorInfo_2_00..05`, `A250822_0/1`).
* Texts: string tables 0x00a4f070 (32), 0x00a4f3f0 (8), 0x00a4f4d0 (6), built by the static initialiser
  at 0x0075b032 from `lang.txt` keys. The legacy keys `gp1..gp56` (servo/encoder/dual drive per axis,
  network, FPGA, e-stop gp16, hard/soft limits gp17..32, follower gp33..43, laser gp44..56) are listed
  by `FUN_006f29e0` but not decoded from a word in this build.
* Any non-zero word = "alarm present"; during a job MainApp pauses (`FUN_005b9a60`) or stops
  (`FUN_005ba0d0` OnStopBtn).
* **Reset**: "Clear Alarm" (gp90) `FUN_004b8690` → CNCModule +0x1e4 = `[9999, 5, 0, 0]`, then clears
  codes 1000..1015 and 150..158 locally; servo codes 1..4 add the `mf223` go-origin prompt, dual-drive
  9..12 `mf435`; code 8025 (bus fault) also writes 100 := 8888 (VM +0x168).
* Follower collision / gas pressure / laser alarms come from the follower and laser devices (+0x1e94,
  +0x1e98, +0x1e9c words), not from the MC words (owner E).

Rebuild: `mlaser.controller.alarms` (`decode`, `from_status`, `Alarm`), `MCStatus.alarms()`,
`UiController.active_alarms`, `signals.alarmsChanged`, `add_alarm_listener`, pause on alarm,
`clear_alarms()` → `[9999, 5, 0, 0]`. Simulator: `set_limit`, `set_estop`, `set_servo_alarm`,
`soft_limits`, `starvation_alarm`; an alarm stops motion and the program, motion commands are refused
while alarms are active, reset clears latched alarms whose input is gone.

## 14. Pause, resume, stop, breakpoint (O, corrects the earlier INFERRED 103 := 3/2)

* `send stop manu cmd` (FUN_10046420, VM +0x48) and `send pause manu cmd` (FUN_10046560, VM +0x4c)
  both post **event 10** `[0x18]`; FUN_100466a0/FUN_10046790 post 11/12. Only VM +0x48 is reachable
  from CNCModule (+0x58).
* VM thread `FUN_100571f0` events 10/11: with no program running ("vm stop jog") → FUN_10054900
  (`[1, 0x1f, 2, decel, 200000]` or 103 := 3 when VM+0xdc == 1), `[103, ZFUpSpeed·10,
  ZFDockHeight·1000]` only when the follower state is 0x75, `[118, 5, 0]`. With a program running
  ("vm stop manu") → state := 0x18, FUN_10054900, PWM off `[9999, 3, PtLaserFreq, 0, 0]` and
  `[9999, 17, …]`, process outputs off `[9999, 2, 0xffff, outputs & ~(laser, gas, …)]` (+ 13 for the
  extension board), follower lift/stop for event 10, `[118, 5, 0]`. Event 12 = FUN_10054900 only.
* MainApp **Pause** = `FUN_005b9a60` "Sys Pause": job state +0x484c := 11 and CNCModule +0x58, i.e. the
  same program stop. **Stop** = `FUN_005ba0d0` "CManuPanel::OnStopBtn". Continue = restart from the
  breakpoint, not a FIFO restart.
* Breakpoint: `FUN_004693c0` writes AutosaveParam1/2.ini while machining: counter, X/Y µm, and the two
  ints of NC vtable +0x278 = VM +0x138 `FUN_10039820` = VM+0xec/+0xf0 = **RO[23], RO[24]** (the RO
  mirror is at VM+0x90). Their exact semantics on the card are U (I: graph and path index).
* Backward/Forward (mp61/mp62, length mp65): wire sequence U.

Rebuild (`UiController`, `mlaser/nc/resume.py`): `pause_mode="breakpoint"` (default) does the program
stop above, takes the executed wire index (FIFO byte accounting) as the breakpoint, writes AutosaveParam
(graph_index = job marker ordinal, path_index = items since it — rebuild convention), and `resume()`
streams `resume_job()`: laser-off rapid to the breakpoint, replay of output/PWM/DA/laser-sync state,
`compiler.resume_items()` (re-opens gas, follower on, laser on) and the rest of the wire.
`step_backward/forward` replay the path's own step deltas (reversed for Backward) with duty 0.
`pause_mode="fifo"` keeps 103 := 3 / 103 := 2 (exact continuation, I). Stop = program stop + 103 := 3
+ 103 := 1.

**停止 (mp56) with no job (O, D-29).** OnStopBtn FUN_005ba0d0: with job state 0 and the follower
link up (NC +0x34) CNCModule +0xe8 `[101]`; then CNCModule +0x58 → event 10 → "vm stop jog"
(§4: `[1, 0x1f, 2, decel, 200000]`, 103 := 3, 103 := 1). Capture: `[101]`, `[1, 31, 2, 4000,
200000]`, 103 := 3, 103 := 1. With a job running the VM does "vm stop manu" (above); job state 2/3
with cfg +0x48c4 adds NC +0x164 (gate off). Rebuild `UiController.stop`: running job → the program
stop with PWM off at LC.PtLaserFreq; idle → `[101]` + `_vm_stop_jog`; paused at a breakpoint →
`_vm_stop_jog` only.

## 15. FIFO capacity, underrun and streaming robustness

* No capacity constant exists in NCModule: fillFifo only compares RO[16] (free bytes) with
  frame bytes + 2000. ipAdd.ini `MaxItemPerFrame` (default 160, stored /2 at +0x750) and `MaxFillItem`
  (default 1000, +0x754) are read with the other ipAdd.ini keys; their use was not traced (U). The rebuild measures
  the capacity as RO[16] right after 103 := 1 (simulator default 64 KiB ≈ 5.4 s of 1 ms steps).
* Start (mcCoreProcess `FUN_10058030`): one fillFifo (≤ 20 frames ≈ 2000 step items ≈ 2 s at 1 ms)
  then `[9999, 1, mask|0x66800000, 0]` and 103 := 2.
* Underrun: the card raises **RO[7] bit 5 "FIFO starvation"**; MainApp then pauses like for any alarm.
* Rebuild `nc/stream.py`: `JobStreamer` prefills until the FIFO is full (or `prefill_ms`), tracks
  buffered motion (`BufferStats.min_buffer_ms`), and a **watchdog** (`watchdog_ms`, 40 ms) stops the
  FIFO and switches the PWM off before the card runs dry, refills and restarts (`auto_resume`).
  `ProcessStreamer` runs it in a spawned process with its own socket (queues for progress and
  pause/resume/cancel); `UiController` uses it for real cards (`stream_process=None` = auto).
  `tools/bench_stream.py` reports the headroom. Measured (simulator in its own process, 2 CPU-hog
  threads in the caller, real time, 64 KiB): thread min fill 4487 ms, process 5182 ms of 5461 ms;
  stress (20× real time, 16 KiB, 4 hogs): thread 48 underruns, process 0 underruns, min 755 ms.

## 16. Edge seek (寻边, mf19)

Tags O/I/U as in §10. MainApp addresses unless marked NC. Implementation:
`mlaser/controller/edgeseek.py`; the simulator's `SimSheet`; `mlaser/nc/compiler.placed_document`.
Tests: `tests/test_machine_edgeseek.py`.

**Commands (O, dispatcher FUN_004b0760).**

| id | lang | handler |
|---|---|---|
| 0x10cd | newLang27 disc search | dialog at frame +0xf3ec (FUN_0051db70) → FUN_005c1ff0 → step list FUN_005c2060 |
| 0x10ce | A250211_0 capacitive | EdgeSeekAuto dialog (FUN_005529e0, init FUN_00559210); Start FUN_00557fd0 → FUN_005c0ad0(3 or 4 from the radio buttons 12000/12001) |
| 0x10cf | A250211_1 manual two-point | CEdgeSeekDlg (FUN_005e17e0): P1/P2 = current head position (NC +0x1a4) into cfg+0x4bc8/+0x4bd8, mode cfg+0x4c48 := 0, angle FUN_005e30c0, OK FUN_005e2ba0 |
| 0x10d0 | A250211_2 memorised angle | shows cfg+0x4c60 in degrees (FUN_004b0990) |
| 0x10d1 | A250211_3 clear | if cfg+0x4bc4 and \|angle\| > 0.0015: CAD +0x2a0; cfg+0x4bc4 := 0; CAD +0x1bc (FUN_004b0420) |
| 0x10d2 | A250211_4 recover | cfg+0x4bc4 := 1; FUN_004b0300: CAD +0x2a0, CAD +0x1b8(cfg+0x4c60, cfg+0x4bc4, cfg+0x4980/+0x4988) |
| 0x10d3 | A250211_5 recover and go to zero | FUN_005b7f20 (corner + move) then as 0x10d2 |

**Step list (O).** FUN_005bfed0(type, a..h) appends 9-int records to panel +0x1f38; the panel
thread FUN_005c35f0 runs them while cfg+0x4848 == 1. Capacitive builder FUN_005c0bd0(start, final,
n), for the start from GRP.EdgeSeekIsCurrentStart (current position, NC +0x1ac) or
GRP.EdgeSeekStartPointX/Y:

1. Signs from MSC.DockPtType (switch at 0x5c0c53): 1 → X dir 1, Y dir 0; 3 → 0, 0; 7 → 1, 1;
   9 → 0, 1; others: message box and abort. `s = dir ? +1 : −1`.
2. `dx = EdgeBoardSizeX·EdgeSeekXPercent·sx`, `dy = EdgeBoardSizeY·EdgeSeekYPercent·sy`. Points
   (n = 3/4): (x0, y0), (x0+dx, y0), (x0, y0), (x0, y0+dy); n = 7: (x0,y0), (x0+dx/2,y0),
   (x0+dx,y0), (x0,y0), (x0,y0+dy/2), (x0,y0+dy), (x0,y0+1.5·dy) (DAT_008c48d8 = 1.5).
   cfg+0x4c48 := n.
3. Per point: [unless current start for point 0] step 0xb (move to the point: relative move by
   target − current, speed MC.XFastMoveSpeed·ppm, acc MC.XFastMoveAcc), step 6 (Sleep 100),
   step 2/0xb (wait: NC +0x280(3), +0x230 == 0); step 0x15 → FUN_005b6a70: NC +0xe8 (write 101 :=
   101), NC +0x110 = mailbox `[117, ftol(ftol(ZFFollowSpeed)·10), ftol(EdgeSeekFollowHeight·1000),
   ftol(EdgeSeekUpHeight·1000), EdgeSeekSensitivity, ftol(SlowEdgeSeekMoveOutTolerance·1000), 0,
   ftol(EdgeSeekOutSpeed·10), 1]` (NC FUN_10031cc0 asm 0x10031cfe..0x10031dba, constant 10.0 at
   0x10087fe8); step 2/0xd → FUN_005b6680(1): ready when follower reg 3 == 0x75 and
   (reg 2 & 0xff) == 3 (with argument 0 the command is 0x68, used by step 2/6); step 9 → relative
   move `−5000·s` mm (DAT_0088e520) along Y for points < 2 (< 3 for n = 7), along X for the others,
   at ftol(EdgeSeekOutSpeed·ppm); step 2/9 → FUN_005b6b30(i, 1); step 2/0xb.
4. Step 0x29 (FUN_005b7f20), step 6 (200 ms), step 2/0xb, step 0x2a(final).

**Edge capture FUN_005b6b30 (O).** Needs ZF.ZFType ≠ 0 and the follower connected (NC +0x34). NC
+0x284 (refresh follower status), `getReg(7, 2) == 0x80000006` (−0x7ffffffa) means "edge found";
then NC +0x64 `[1, 0x1f]` (stop), `getReg(7, 14)`, `getReg(7, 15)`, NC +0x280(3), latched position
`getReg(3, axis·10 + 7) / ppm` for X and Y. With `start` = cfg+0xb4b8 (position stored by step
2/6) and `v = latch − start`: if |v.y| ≤ |v.x| then `x = start.x + sign(v.x)·L` else
`y = start.y + sign(v.y)·L`, with `L = GRP.EdgeSeekXYSlowSpeed · 0.00025 · reg15`
(DAT_008c48c8). The point goes to cfg+0x4bc8 + 16·i and is logged with mp88_3. I: reg 15 is the
number of 0.25 ms samples from the start of the move to the edge; note the original uses
EdgeSeekXYSlowSpeed (+0x45a8) for the conversion although the move runs at EdgeSeekOutSpeed
(+0x47a0); both default to 50. A debug dump of reg 14 records goes to `D:\seekEdge.txt`
(FUN_005b7330, step 2/10) and is not reproduced.

**Corner FUN_005b7f20 (O).** n = 4: intersection of lines p0p1 and p2p3 (FUN_00475b60); n = 3:
projection of p2 on p0p1 (FUN_00475c10); n = 7: robust fit — the four left points are rotated
−π/4 about p3 (FUN_005d42b0), leave-one-out least squares (FUN_00475800, threshold 1e-5),
smallest residual (FUN_00475900) kept, bottom points fitted likewise, then projection FUN_00475a90
(the rebuild intersects the two fitted lines, I). Corner → cfg+0x4c38 (mp88_4). Then a relative
move (FUN_005be7e0, XFastMoveSpeed/Acc) to `corner + (sx'·EdgeOffsetX, sy'·EdgeOffsetY)` −
current, `sx' = (EdgeSeekXDirection == 1)·2 − 1`.

**Angle, step 0x2a (O, 0x5c58ab..0x5c5b89; same code in CEdgeSeekDlg FUN_005e30c0).**
`v = p1 − p0`, negated if v.x < 0; `a = atan2(v)` (FUN_0055a090 against (1, 0)) if |v.x| > 0.1
else 0. n = 4: `w = p3 − p2`, negated if w.y < 0, `b = atan2(w)` if |w.y| > 0.1 else π/2; if
`|(b − a) − π/2|·180/π > 0.02` then `a += ((b − a) − π/2)·0.5`. cfg+0x4c60 := a, cfg+0x4bc4 := 1,
CAD +0x2a0, CAD +0x1b8(angle, flag, cfg+0x4980) — the anchor point of the job placement (§17) is
the rotation centre. With final == 0 (the capacitive dialog) step 0x2a only posts message 0x2b10
to the dialog, whose OK (FUN_00557d90) applies the angle.

**Disc search (O, FUN_005c2060).** Three probes from the guessed centre: move out by (−2000, 0),
(+2000, 0), (0, −2000) mm (DAT_0084cc70) with the same 0x15/2-0xd/9 steps; capture step 2/0xf →
FUN_005b7950 (status 0x80000006, latched X, Y and the ZF axis cfg+0x4ab8, no correction) into
cfg+0x4c78 + 24·i (newLang34); step 0x43 (FUN_005b8ef0) = centre of the circle through the three
3-D points → cfg+0x4cc0, move there; step 0x44: rotation flag on, CAD +0x1b8, and when the dialog
check box cfg+0x4cd0 is set, set the work origin there (FUN_005c9d50 for tool 1..6,
FUN_005ca650 for manual).

**Where the transform lands (rebuild).** `edgeseek.apply_to_document` writes the .chf whole-drawing
rotation (`extra["rotated"]`, `rotate_angle`, `rotate_center`, `rotate_offset`; formats.md §2.1,
CADModule FUN_100bf4a0) and `compiler.compile_job` applies it through `placed_document` when
|angle| > 0.0015 (CADModule FUN_100bd2bc). `UiController.place` centres it on the job origin.

**Simulator (I).** `CardSimulator(sheet=SimSheet.rect(x0, y0, w, h, angle_deg))` or
`SimSheet.disc(...)`: mailbox 117 arms (follower reg 3 = 117, reg 2 = 0x80000003), the first
inside→outside crossing of the head latches axis RO k7, sets reg 15 = reg 14 = distance from the
move start / (out speed · 0.00025 s) and reg 2 = 0x80000006; write(101, 101) disarms. The axes keep
moving until the PC stops them.

**Unknown.** The meaning of follower reg 14 beyond "samples" (the dump writes pairs); the exact
mode-7 final projection; the CAD +0x1b8 offset argument (0 in the rebuild); how the card uses
words 2..8 of the 117 command internally.

## 17. Coordinate systems (mf307)

Implementation `mlaser/controller/coords.py`, `MachineOps.select_coord/set_origin/return_origin`,
`UiController.coords` / `place`. Tests `tests/test_machine_ops.py`.

* O: ids 0xcf6d..0xcf74 → FUN_004b1130 → FUN_00482950: MSC.CoordSysType (+0x42a0) := id − 0xcf6d
  (0 Float mf300, 1..6 Tool mf301..306, 7 Manual mf513); labels FUN_004829b0 (Manual shows
  mp163/mp164 instead of mp129/mp130). 0xcf75 set origin FUN_004b1200, 0xcf76 return FUN_004b1270,
  enabled only for 1..7 (FUN_004b1150).
* O storage: tool origins `cfg+0x42c0 + 16·i` (X, Y mm); ManuPara.xml `MSC.ToolCoordPtStr` =
  `"x1,y1,…,x6,y6"` (writer FUN_005cd770, lexical_cast doubles; reader FUN_004d90e0 at 0x4dbb9a,
  12 tokens or all zero); mark points `+0x4238`, `MSC.MarkPtStr` (FUN_005cd520). Not in
  softPara.ini or AutosaveParam.
* O set (FUN_005c9d50): guards (panel idle, state 0, connected); position NC +0x1a4
  (FUN_1002c6a0: axis RO k2 / cfg+0xbc28, skew FUN_100386c0); tool[n−1] := (X, Y); mp131.
* O return (FUN_005ca160): Δ = tool − cached position (panel +0x1e40/+0x1e48); speed 100 when
  the tool origin is (0, 0) (≤ 0.001), else MC.XFastMoveSpeed; acc MC.XFastMoveAcc (not ·ppm);
  optional ZF home first (MP.IsZFGoOriginAfterDone); step 9 → FUN_005bea00 → NC +0x50
  `[5, all axes, ftol(v·ppm), acc, 10·acc, ftol(Δx·ppm), ftol(Δy·ppm), 0, 0]` after the inverse
  skew NC +0x2f4.
* O manual: select (FUN_004829b0, Manual branch) → CNCModule +0x2a4(0, 0) = axis switch words
  rewritten with +0xbc76 := 1 (§11a; capture 50200/50240/50280/50360 := 131088); set
  FUN_005ca650 → NC +0x1a8 FUN_100314b0 = `[9999, 8, axis mask]`
  (FUN_004b0060(cfg+0xbbd0)), clear per-axis offsets cfg+0xbccc+axis·0xa0, NC +0x198 (soft
  limits, §11a: capture 50202/50242 := [−500, 1000000]), mark points := 0; return FUN_005ca730:
  to (0, 0) at min(XFastMoveSpeed, 100).
* O mark points (D-24): 标记 (mp39, OnMarkPtBtn FUN_005c9390): mark[MC.MarkPtIndex] := head
  position (NC +0x1a4), flag cfg +0x4298+n := 1, mp88 message, MSC.MarkPtStr saved, no traffic;
  返回 (af10, OnReturnMarkPtBtn FUN_005c97f0): relative move to the mark (§11a).
* O 回零位 (mp29, FUN_005bc220, D-25): tool coordinate system 1..6 and X|Y not both homed
  (NC +0x224 < 3) → mp133 prompt → sysGoOrigin; otherwise target = first point of the panel's job
  list (FUN_00469890) or (0, 0); for (0, 0) the mp79 prompt, then the move (§11a). Rebuild
  `UiController.return_zero(confirm, target)` (target None = (0, 0)).
* O placement FUN_0045cc20(pos): origin = tool[n−1] (1..6) or the head position (Float,
  Manual); CAD +0x198 translates by origin − anchor(cfg+0x4980), then cfg+0x4980 := origin. The
  anchor FUN_004d2750: MSC.DockPtType 1..9 on the bbox (X: %3 == 1 min, 2 centre, 0 max; Y: 1..3
  max, 4..6 centre, 7..9 min; 0 none); with an edge-seek angle the rotated rectangle corners.
* Rebuild: `UiController.coords = None` keeps the phase-1/2 behaviour (drawing coordinates =
  machine coordinates); selecting a coordinate system in the UI enables the placement
  (`extra["job_offset"]`, applied by `placed_document`). I: the mask of `[9999, 8, mask]` is X|Y.

## 18. Laser type and focus go-origin

* O gp120 menu (built only when SP.m_iHardwareModel +0x472c == 0xe0): ids 0x26c0 fiber, 0x26c1
  CO2, 0x26c2 blue → FUN_004b12e0: emission off NC +0x170 (FUN_10030f70: type outputs, then the
  PWM off pair `[9999, 3|17, LC.PtLaserFreq, 0, 0]`), gate/DA off +0x164 (FUN_1002db10), red
  light off +0x178 (FUN_1002ddb0; outputs of the old type: LGP.DOLaser/DORedLight or
  LGP.CO2DOLaser/CO2DOLaserGate/CO2DORedLight), DA off when MP.LaserDAKeepOutput and
  LGP.LaserDAPort; SP.m_iEnableLaserType (+0x4728) := id − 0x26c0; fiber: DA off again,
  `[118, 5, 0]`, LGP.doCO2EnableOutput := 0, A250607_0; CO2: `[118, 5, 1]`, output := 1,
  A250607_1; blue: name only; NC +0x198 soft limits (§11a; CO2 second soft limit when
  MP.CO2EnableSecondSoftLimit). `[118, 5, x]` goes through NC +0x84 → VM +0x70 (FUN_100390c0),
  which drops it without a follower: none in the capture (ZF.ZFType 0). No restart. Capture
  (D-32): fiber → CO2 `[9999, 3, 1234, 0, 0]`, `[9999, 17, 1234, 0, 0]`, `[9999, 2, 8, 0]`,
  50202/50242 := [−500, 1000000]; CO2 → fiber the same without the red light (LGP.CO2DORedLight 0). The layer records follow the type (FUN_00458c60:
  +0x4ed8 fiber, +0x8058 CO2 and blue). tempIsBreak.ini stores the type (A250421_0 on mismatch).
  Rebuild: `MachineOps.switch_laser_type`, the UI swaps `document.layers` between the per-type
  tables (CO2 defaults from LayerPara `CO2LayerParam*`). I: `[118, 5, x]` is a follower/laser
  mode select rather than "follower stop" as §4 reads it.
* O A240827_1 (id 0xbc4 → FUN_004aee70 → CAutoFocusView::GoOriginBtn FUN_004fbeb0) by AF.AFType:
  1/2 AF reg 101 := 2; 3 EC `[2, dir]`; 4 IO (EnableMovePort := 0, GoOriginPort 0→1, Sleep
  FocusGoOriginDelay, → 0, DA 0); 5 MC `[2, 0x10, 0]`; 6 MC `[2, 1 << AF.AFAxis, 0]`; 7 VM +0x200
  (U, not implemented). Rebuild `MachineOps.home_af`.

## 19. Firmware upgrade and restart

Implementation `mlaser/controller/upgrade.py`, UI `ext_machine` (refuses without a connected
card, checks the file, confirmation by typing UPGRADE). Tests against the simulator only.

| command | id / handler | wire |
|---|---|---|
| mf132 MC (*.mcf) | 0x32cd FUN_004c0500 (O) | file ≥ 14 bytes (0x4c0a3e) else mf176; NC +0x248 → FUN_10046020 (MC rx timeout 2000 ms, event 9); FUN_10055840: chunks of 1400 (0x578) → FUN_10053930 **func 0x26 `[addr = file offset, n, bytes]`**; on error "false fillDownFile", stop; progress offset·100/size (VM +0x108); at 100 %: NC +0x24c (timeout back), NC +0x1ec = write(100, 8888); 10 s countdown, mf179 |
| automatic | FUN_00594fe0 (O) | `0 < RO[1] < MinHardwareVer` → mf232 → `Update\MCC100_V%.2f.mcf` |
| mf440 AF (*.afb) | 0x32d4 FUN_004c0d00 (O) | AF reg 100 := 8888 (RTU), Sleep 500, NC +0x260 → event 0x14 FUN_10055a60: RTU func 0x26 chunks of MaxAFDownItem (200), Sleep 2000 after the first, AFUpdateInterval after the others; finish reg 100 := 6666, Sleep 1000, mf439, reconnect |
| mf625 extension (*.efb) | 0x32d5 FUN_004c1500 (O) | same on the EC HAL (event 0x18, FUN_10054390, MaxECDownItem 200); finish 6666, mf624 |
| mf131 restart | 0x32cc FUN_004b8740 (O) | write(100, 8888) only |

O: no erase, start or commit command; the PC never parses the header. I: `.mcf` word 0 is the file
length (0x00019200 = 102912 in the shipped file); the rebuild refuses a mismatch (safety check, not
in the original). U: how the card validates the image; the on-board follower loop (event 0x10,
FUN_10055ec0, func 0x26 on the MC HAL, 512-byte chunks) has no MainApp caller in this build, so
mf440 is the AF upgrade and there is no follower firmware path in the rebuild.

## 20. Offline process (A241115_1)

O (id 0x26b1, FUN_00487f30): not a download to the MCC100. Save dialog `*.enc` (laser type 0) /
`*.encex`; sys+0x4730 := 1 and the normal process generation runs; NC +0x68 (FUN_100352b0) then
writes `_tempManuItem.olpf` instead of the FIFO: `Manu_Begin %d %d`, one line per ManuItem
`+0x18 +0x16 +0x14 +0x12 +0x10 +0x0 +0x4 +0x8 +0xc`, `Manu_End`, `Size %d`. Container
(FUN_0048e590/FUN_0048e980): `[olpi]NEXCUT_OLPI_END[olpf]NEXCUT_OLPF_END[jpg]NEXCUT_JPG_END
[layer xml]NEXCUT_LAYER_XML_END[chf]NEXCUT_CHF_END`. Optional HTTP upload to a NexCut controller
(`POST /NexCut/File/UploadGCode`, `/NexCut/File/LoadFile`) — not implemented. U: .olpi content,
encryption. NC also has a card-side offline path (NC +0x318..+0x324: UDP discovery event 0x19,
`[7, "name&extra"]` to reg 101, 1400-byte func 0x26 chunks, `[8]`; FUN_10048ad0) with no MainApp
caller (O). Rebuild: `mlaser/nc/offline.py` (`export_offline`, `upload_offline_to_card`).

## 21. Parameter backup, task files, recovery, network test

Implementation `mlaser/formats/backup.py`, `mlaser/controller/nettest.py`, `ext_machine`.

* Parameter files (O, FUN_00451c30): `%LOCALAPPDATA%\NexCut\{Hard,Manu,Layer}Para.xml`;
  backups `<exe>\File\BkHardPara.xml`, `BkManuPara.xml`, `BkLayerPara.xml`,
  `SecondBkManuPara.xml`. (formats.md still says `dat\`.)
* Card blocks (O): writeHardParam2Card FUN_10050fb0 (CNCModule +0x29c), readParamFromCard
  FUN_10051320 (+0x2a0): HardPara table (457 entries, 0xafb4d8) in 250-entry slots, reg 59600
  (entries 0..249) and 59601 (250..456), `[0xffffffff, 0xe0, values…]`; int types 1, 4, 6, 7, 10..13
  as int32, doubles (2, 9) trunc(v·10000), bools (5) as 0/1, strings 0. No checksum.
* Commands (O): mf480 0xbba FUN_00489540 (password ss11 "3721" I; slots → card; save Hard/Layer/Manu
  + copy to Bk; mf236/mf237); mf481 0xbbd FUN_00489dd0 (password, mf238; read slots; cfg+0xbc28 == 0
  → "Hardware restore failed"; default check FUN_00636ca0 → "save param is wrong"; saveHardParam;
  layer/manu from Bk; mf239/mf240); mf234 0x2724 FUN_0048a710 (all tables into one XML); mf235
  0x2725 FUN_0048ae20 (hard + manu, A250107_0 for the layers). Start-up FUN_005ba6e0: default
  hardware parameters → mf441, read from the card, mf442/mf443.
* Task `.aut` (O, save 0x26b4 FUN_0048d250, import FUN_0048c550): see `formats/backup.py` module
  doc. The import id 0x26b3 is shared with "File upload" (A250529_0) in the message map (O); the
  rebuild binds file.task_import to the .aut importer.
* Crash recovery (O): FUN_00596320 reads softPara.ini `[SC2000] NormalExit`; 0 → mp18, then
  FUN_00480860 loads `File\autosave.chf` and resumes from AutosaveParam; NormalExit := 0, the exit
  path writes 1. autosave.chf is written by the thread FUN_004804a0 when the machining preparation
  FUN_00457de0 finds the drawing modified. mf138 System Recovery 0x3e84 FUN_004bb100 = mf167
  confirm + the same reload (no parameter restore, despite the text). Rebuild: job-start hook
  writes autosave.chf; startup logs mp18 instead of a modal prompt.
* Network test (O, mf436 0x32d3 FUN_004c1ed0): `cmd.exe /k ping 10.1.1.168 -l 1000 && ping
  10.1.1.169 -l 1000`; pingMC.bat/pingZF.bat are dead strings. Rebuild (I): UDP probes — a func
  0x30 read of RO[0..1] to the card, a Modbus read to the follower address — with loss and RTT.
