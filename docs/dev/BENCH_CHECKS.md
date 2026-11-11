# Bench checks

Behaviour of the real motion card that the simulator has to assume, with the procedure that
settles it on the machine. Each check runs **with the laser physically disabled**; none of them
needs the beam. Record the result (date, firmware version from the card's RO[1], the capture
file name) in [protocol.md](../re/protocol.md) and switch the simulator to what the card did.

## Before every check

1. Laser key off and the laser source switched off. No sheet on the table. Head in the middle of
   the table, away from the limits. Emergency stop within reach.
2. Close the original program and mlaser: the check talks to the card on its own.
3. Start a packet capture of the card traffic on the PC (Wireshark with the capture filter
   `udp port 502`, or `dumpcap -i <interface> -f "udp port 502" -w check.pcapng`; details in
   [parity.md §8.2](../re/parity.md#82-capturing)). Keep the capture with the result. It stays
   on your machine: captures are never committed to the repository.
4. Rehearse on the simulator first (`--sim`, below): same output, nothing on the network.

## 1. A FIFO frame received twice

**Why.** When a reply does not arrive in time the client sends the identical request again, as
the original does (retry policy, [protocol.md §1](../re/protocol.md#1-transport)). For a FIFO
frame the card has already queued the first copy, so it receives the same frame, with the same
sequence number, twice. What it does then is unknown:

| model | the card ... | consequence for a job |
|---|---|---|
| `accept-twice` | queues the second copy too | the frame's segment runs twice: extra travel, with the beam on a segment cut twice. The host cannot see it (RO[15] is the same either way) and ends the job as "done". |
| `dedupe` | acknowledges it and drops it | none |
| `reject` | answers with an exception | the stream stops with a FAULT; Resume continues from the breakpoint |

What the host does in each case is pinned by `tests/safety/test_duplicate_fifo_frame.py`. On the
real card's configuration (process streamer, 150 ms link deadline) a lost FIFO reply already
latches a FAULT before the re-send (the FIFO timeout is 600 ms), and after that halt the frame is
not sent again (the client asks the supervisor before every re-send of a motion request). With the in-thread streamer and the simulator's 1 s
deadline the re-send reaches a running card, which is where the model matters. The simulator
default is `accept-twice`, the worst case, until this check is done.

**Procedure.**

1. The preparation above. Power the card, home it (with the original or mlaser), close the
   program again.
2. Rehearse: `uv run python tools/bench_duplicate_frame.py --sim accept-twice` (and `dedupe`,
   `reject`) shows what each answer looks like.
3. On the card, without any motion: `uv run python tools/bench_duplicate_frame.py --card
   10.1.1.168 --laser-disabled`. The tool checks that the card is idle with no alarm, switches
   the PWM off, sends `[9999, 16]` and a FIFO clear, then one frame of 50 step items (duty 0)
   twice as the same datagram, reads the replies and the free FIFO bytes (RO[16]) after each
   copy, and clears the FIFO again. The FIFO is never started.
4. Optional confirmation with motion: add `--run`. The tool then starts the FIFO and measures the
   X travel: 200 pulses (2 mm at 100 pulses/mm) when the frame ran once, 400 when it ran twice.
   Every step has duty 0; the PWM goes off before and after.
5. Stop the capture.

**Reading the result.** The tool prints the replies to the two copies, RO[16] before and after
each copy, RO[15], and a verdict with the simulator model to select:

```
replies to the two copies: ['ok', 'ok']
free FIFO bytes (RO[16]) before / after copy 1 / after copy 2: [65536, 64936, 64936]
verdict: dedupe  ->  simulator default: duplicate_fifo='dedupe'
```

Exit status 1 (no verdict) means the answers fit no model: for example the second copy was
acknowledged and RO[16] dropped by a different amount. Then look at the capture:
`uv run python tools/compare_capture.py check.pcapng --list` lists the FIFO jobs (the two copies
are one exchange with a retransmission), `--dump frames.json` writes the decoded stream, and
`MLASER_CAPTURE_DIR=<folder> uv run pytest tests/test_capture_replay.py` replays the capture
through the decoder and the documented invariants ([Testing](TESTING.md#replaying-your-own-captures)).

**Afterwards.** Set the verdict as the default of `CardSimulator(duplicate_fifo=...)` in
`mlaser/controller/simulator.py` and note the result in
[protocol.md §15](../re/protocol.md#15-fifo-capacity-underrun-and-streaming-robustness). If the
card is `accept-twice`, the host has to learn to notice a re-sent frame (for example compare
RO[16] with the bytes it sent before it re-sends a FIFO frame). That changes the wire traffic and
needs its own review; until then run the machine on a direct cable, without switches or Wi-Fi
between the PC and the card.

## 2. The program flag after a pause

**Why.** A breakpoint pause sends the program stop (decelerated stop, PWM off, outputs off,
follower stop) and then a FIFO clear, but no FIFO stop (103 := 3). The start gate refuses Resume
while the card reports a program (RO[19] low byte 1). The owner's captures show the low byte
going to 0 after 103 := 3; what a FIFO clear alone does is not recorded. The simulator assumes
the clear ends the program.

**Procedure.** The preparation above, then in mlaser a Dry Cut (laser disabled) of a large
rectangle; press **Pause** after a few seconds, wait until the head stopped, press **Resume**.

**Reading the result.** If the job continues, the assumption holds. If Resume is refused with
"the card is running a program: wait until it is idle", the card keeps the flag after a clear:
keep the capture, the device report and the log files
([how to report a problem](../user/faq.md#how-do-i-report-a-problem)) and report it; the pause
then needs a FIFO stop before the clear.
