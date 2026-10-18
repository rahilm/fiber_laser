"""Capture comparison tool (mlaser.parity, tools/compare_capture.py).

Synthetic captures are made from the rebuild's own simulator traffic (mlaser.parity.synth),
written with the pure-Python pcap/pcapng writer, read back and compared with compile_job:
a perfect match must be reported as IDENTICAL; injected differences (dropped step, changed
duty, swapped process items) must be detected and located.
"""
from __future__ import annotations

import importlib.util
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from mlaser.protocol import frames as F  # noqa: E402
from mlaser.formats.params import MachineParams  # noqa: E402
from mlaser.parity import capture as C  # noqa: E402
from mlaser.parity import pcap as P  # noqa: E402
from mlaser.parity import report as R  # noqa: E402
from mlaser.parity import synth as S  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
START = (5.0, 3.0)


def _tool():
    spec = importlib.util.spec_from_file_location("compare_capture", ROOT / "tools" / "compare_capture.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    d = tmp_path_factory.mktemp("cap")
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (30, 0), (30, 20), (0, 20)], close=True)
    msp.add_circle((15, 10), 4)
    dxf = d / "part.dxf"
    doc.saveas(dxf)
    dat = d / "NexCut"
    MachineParams.load_dir(d / "none").save_dir(dat)
    inp = C.RebuildInputs(str(dxf), str(dat))
    m, _ = C.make_machine(inp)
    job = C.compile_rebuild(inp, m, START, False)
    msgs = S.to_messages(S.record_job(job, START))
    return {"dir": d, "dxf": str(dxf), "dat": str(dat), "inp": inp, "machine": m, "job": job, "msgs": msgs}


def _write(setup, msgs, name="c.pcap", **kw) -> str:
    p = setup["dir"] / name
    P.write_capture(p, msgs, **kw)
    return str(p)


def _run(setup, msgs, name="c.pcap", inp=None, **kw):
    return C.run(_write(setup, msgs, name, **kw), inp or setup["inp"])


# ----------------------------------------------------------------------------- pcap layer
@pytest.mark.parametrize("fmt,lt,ns", [("pcap", P.LINKTYPE_ETHERNET, False), ("pcap", P.LINKTYPE_LINUX_SLL, True),
                                        ("pcapng", P.LINKTYPE_ETHERNET, False), ("pcapng", P.LINKTYPE_NULL, True),
                                        ("pcap", P.LINKTYPE_LINUX_SLL2, False), ("pcapng", P.LINKTYPE_RAW, False),
                                        ("pcap", P.LINKTYPE_LOOP, False)])
def test_pcap_roundtrip(tmp_path, fmt, lt, ns):
    msgs = [P.Message(1_700_000_000.123456 + i * 0.001, ("10.1.1.100", 5000), ("10.1.1.168", 502),
                      bytes([i]) * (i + 1)) for i in range(5)]
    msgs.append(P.Message(1_700_000_001.0, ("10.1.1.168", 502), ("10.1.1.100", 5000), b"\x00\x01reply"))
    msgs.append(P.Message(1_700_000_001.5, ("10.1.1.100", 5000), ("10.1.1.9", 80), b"other"))   # filtered
    p = tmp_path / f"x.{fmt}"
    P.write_capture(p, msgs, fmt=fmt, linktype=lt, nanosecond=ns)
    st = P.ReadStats()
    got = P.read_messages(p, 502, None, st)
    assert [m.data for m in got] == [m.data for m in msgs[:6]]
    assert [m.src for m in got] == [m.src for m in msgs[:6]]
    assert all(abs(a.ts - b.ts) < 2e-6 for a, b in zip(got, msgs))
    assert st.packets == 7 and st.udp == 6 and st.linktypes == {lt}


def test_ethernet_vlan_padding_and_fragments(tmp_path):
    src, dst = "10.1.1.100", "10.1.1.168"
    small = P.udp_segment(src, dst, 5000, 502, b"abc")
    vlan = (b"\x02\0\0\0\0\x02\x02\0\0\0\0\x01" + struct.pack(">HHH", 0x8100, 7, 0x0800)
            + P.ipv4_packet(src, dst, P.PROTO_UDP, small, 1) + b"\0" * 20)      # VLAN tag + trailer padding
    big = P.udp_segment(src, dst, 5000, 502, bytes(range(256)) * 8)
    f1 = P.ipv4_packet(src, dst, P.PROTO_UDP, big[:1000], 9, 0, True)
    f2 = P.ipv4_packet(src, dst, P.PROTO_UDP, big[1000:], 9, 1000, False)
    p = tmp_path / "v.pcap"
    with P.PcapWriter(p) as w:
        w.write(1.0, vlan)
        w.write(2.0, P.ethernet_frame(f2))          # fragments out of order
        w.write(2.1, P.ethernet_frame(f1))
    got = P.read_messages(p)
    assert [m.data for m in got] == [b"abc", bytes(range(256)) * 8]


def test_tcp_reassembly(tmp_path):
    """AccessType=0: frames split over segments, out of order, retransmitted and overlapping."""
    reqs = [F.encode_request(F.Request(F.FUNC_WRITE, 102, 3 + 20 * i, list(range(3 + 20 * i)), tid=i))
            for i in range(4)]
    stream = b"".join(reqs)
    c, s, sp, dp = "10.1.1.100", "10.1.1.168", 49152, 502
    isn = 0xFFFFFF00                                       # sequence numbers wrap inside the stream
    segs = [(isn + 1 + o, stream[o:o + 70]) for o in range(0, len(stream), 70)]
    order = list(range(len(segs)))
    order[2], order[3] = order[3], order[2]                # out of order
    p = tmp_path / "t.pcapng"
    with P.PcapngWriter(p) as w:
        w.write(0.5, P.ethernet_frame(P.ipv4_packet(c, s, P.PROTO_TCP, P.tcp_segment(c, s, sp, dp, isn, 0, P.TCP_SYN))))
        t = 1.0
        for k in order:
            sq, data = segs[k]
            w.write(t, P.ethernet_frame(P.ipv4_packet(c, s, P.PROTO_TCP,
                                                      P.tcp_segment(c, s, sp, dp, sq, 1, P.TCP_ACK, data))))
            t += 0.01
        sq, data = segs[1]                                 # retransmission of an old segment
        w.write(t, P.ethernet_frame(P.ipv4_packet(c, s, P.PROTO_TCP, P.tcp_segment(c, s, sp, dp, sq, 1, P.TCP_ACK, data))))
        sq, data = segs[4]                                 # overlapping re-send (half old, half old)
        w.write(t + 0.01, P.ethernet_frame(P.ipv4_packet(c, s, P.PROTO_TCP, P.tcp_segment(
            c, s, sp, dp, sq - 10, 1, P.TCP_ACK, segs[3][1][-10:] + data))))
    st = P.ReadStats()
    got = P.read_messages(p, stats=st)
    assert [m.data for m in got] == reqs
    assert all(m.transport == "tcp" for m in got)
    assert st.tcp_retransmitted_segments >= 1


# ----------------------------------------------------------------------------- perfect match
def test_perfect_match_udp(setup):
    res = _run(setup, setup["msgs"])
    cmp = res.comparison
    assert cmp.verdict.startswith("IDENTICAL"), R.text_report(res)
    assert all(c.status in (C.PASS, C.INFO) for c in cmp.checks), [c for c in cmp.checks if c.status not in ("PASS", "INFO")]
    assert res.start_mm == pytest.approx(START)
    assert res.offset_mm == (0.0, 0.0) and not res.dry_run
    assert res.job.steps == setup["job"].steps and res.job.wire == setup["job"].wire
    assert cmp.info["path_hausdorff"] == 0 and cmp.info["max_deviation_pulses"] == 0
    assert res.analysis.position_ppm == 1000 and res.analysis.scaling().step_ppm(0) == pytest.approx(100.0)
    assert cmp.check("card-reported end position").status == C.PASS
    txt = R.text_report(res)
    assert "IDENTICAL" in txt and "job 0" in txt


def test_perfect_match_tcp_pcapng_and_chf(setup):
    from mlaser.formats.chf import save_chf
    chf = setup["dir"] / "part.chf"
    save_chf(C.load_drawing(setup["dxf"]), chf)
    inp = replace(setup["inp"], drawing=str(chf))
    job = C.compile_rebuild(inp, setup["machine"], START, False)
    msgs = [replace(m, transport="tcp") for m in S.to_messages(S.record_job(job, START))]
    res = _run(setup, msgs, "c.pcapng", inp, fmt="pcapng", transport="tcp", tcp_mss=536)
    assert res.analysis.traffic.transport == "tcp"
    assert res.comparison.verdict.startswith("IDENTICAL"), R.text_report(res)


def test_retransmissions_and_resends_tolerated(setup):
    msgs = list(setup["msgs"])
    out, k = [], 0
    for m in msgs:
        out.append(m)
        req = S._is_fifo_write(m)
        if req is not None:
            k += 1
            if k % 3 == 0:          # identical datagram again (lost reply: CMCHalAPI resend, same tid)
                out.append(replace(m, ts=m.ts + 0.0005))
            if k == 5:              # "second fillFifo": the same frame with a new transaction id
                r2 = F.Request(req.func, req.addr, req.count, req.values, tid=60000)
                out.append(replace(m, ts=m.ts + 0.0007, data=F.encode_request(r2)))
    out.sort(key=lambda m: m.ts)
    res = _run(setup, out)
    assert res.comparison.verdict.startswith("IDENTICAL"), R.text_report(res)
    assert res.job.retransmissions >= 5 and res.job.resends == 1
    assert res.analysis.traffic.retransmissions >= 5


def test_dry_run_and_offset_estimated(setup):
    inp = setup["inp"]
    m = setup["machine"]
    off = (4.0, -2.5)
    job = C.compile_rebuild(inp, m, START, True, off)
    msgs = S.to_messages(S.record_job(job, START))
    res = _run(setup, msgs, "dry.pcap")
    assert res.dry_run
    assert res.offset_mm == pytest.approx(off, abs=0.006)
    # the estimate is 0.005 mm off; with FUN_00473bd0's truncation + carry (JB-01) a sub-pulse
    # shift changes single deltas by 1 pulse, so the verdict is EQUIVALENT (cumulative position
    # within 1 pulse) unless the estimate is exact
    assert res.comparison.verdict.startswith(("IDENTICAL", "EQUIVALENT")), R.text_report(res)
    # without the offset estimate the paths differ
    res0 = C.run(str(setup["dir"] / "dry.pcap"), replace(inp, offset_mm=(0.0, 0.0)))
    assert res0.comparison.verdict.startswith("DIFFERENT")
    assert res0.comparison.info["path_hausdorff_mm"] > 2.0


def test_frame_kind(setup):
    from mlaser.nc import compile_items, frame_items
    from mlaser.nc.compiler import placed_document
    m = setup["machine"]
    bb = placed_document(C.load_drawing(setup["dxf"])).bbox()
    job = compile_items(frame_items(bb, m.frame_speed), m, START, dry_run=True)
    res = _run(setup, S.to_messages(S.record_job(job, START)), "frame.pcap", replace(setup["inp"], kind="frame"))
    assert res.comparison.verdict.startswith("IDENTICAL"), R.text_report(res)


def test_stop_sequence(setup):
    m = setup["machine"]
    job = C.compile_rebuild(setup["inp"], m, START, False)
    msgs = S.to_messages(S.record_job(job, START, time_scale=1.0, stop_after_s=0.4, fifo_capacity=12000))
    tr = C.parse_traffic(msgs)
    an = C.analyse(tr)
    assert len(an.jobs) == 1
    stop = an.jobs[0].stop_sequence()
    assert stop and stop[0][1][0] == F.OP_STOP_AXES and stop[-1] == (F.REG_FIFO_CTRL, [F.FIFO_CLEAR])
    res = _run(setup, msgs, "stop.pcap")
    assert res.comparison.check("pause/stop sequence").status == C.PASS
    # a stopped job is (correctly) shorter than the rebuild's full job
    assert res.comparison.verdict.startswith("DIFFERENT")
    assert res.comparison.check("step count").status == C.FAIL


# ----------------------------------------------------------------------------- injected differences
def _wire(setup):
    return [it for _seq, it in S.wire_items(setup["msgs"])]


def test_dropped_step_detected_and_located(setup):
    wire = _wire(setup)
    ss = C.step_stream(wire)
    # a laser-on moving step whose neighbours differ from it: the drop has one possible place
    k = int(next(i for i in range(ss.n // 2, ss.n - 1) if ss.duty[i] and (ss.dx[i] or ss.dy[i])
                 and (ss.dx[i + 1], ss.dy[i + 1]) != (ss.dx[i], ss.dy[i])
                 and (ss.dx[i - 1], ss.dy[i - 1]) != (ss.dx[i], ss.dy[i])))
    wi = int(ss.step_wire_index[k])
    res = _run(setup, S.edit_wire(setup["msgs"], drop=wi), "drop.pcap")
    cmp = res.comparison
    assert cmp.verdict.startswith("DIFFERENT")
    assert cmp.info["capture_steps"] == cmp.info["rebuild_steps"] - 1
    assert cmp.check("step count").status == C.FAIL
    assert cmp.first_divergence["item"] == wi and cmp.first_divergence["run_start"] == wi
    assert cmp.step_divergence["step"] == k and cmp.step_divergence["run_start"] == k
    assert cmp.check("end position").status == C.FAIL
    assert cmp.info["max_deviation_pulses"] >= 1
    txt = R.text_report(res)
    assert f"First step divergence: step {k}" in txt


def test_dropped_step_in_a_run_is_bracketed(setup):
    wire = _wire(setup)
    ss = C.step_stream(wire)
    k = int(next(i for i in range(ss.n // 2, ss.n - 2) if ss.duty[i] and (ss.dx[i] or ss.dy[i])
                 and (ss.dx[i + 1], ss.dy[i + 1]) == (ss.dx[i], ss.dy[i])))
    res = _run(setup, S.edit_wire(setup["msgs"], drop=int(ss.step_wire_index[k])), "drop2.pcap")
    sd = res.comparison.step_divergence
    assert sd["run_start"] <= k <= sd["step"] and sd["step"] > k


def test_changed_duty_detected_and_located(setup):
    wire = _wire(setup)
    ss = C.step_stream(wire)
    k = int(next(i for i in range(ss.n // 3, ss.n) if ss.duty[i]))
    wi = int(ss.step_wire_index[k])
    new = int(ss.duty[k]) - 7
    res = _run(setup, S.edit_wire(setup["msgs"], set_step=(wi, {"duty": new})), "duty.pcap")
    cmp = res.comparison
    assert cmp.verdict.startswith("DIFFERENT") and "duty per step" in cmp.verdict
    assert cmp.check("duty per step").status == C.FAIL
    d = cmp.laser_divergence["duty"]
    assert d["step"] == k and d["capture"] == new and d["rebuild"] == ss.duty[k]
    assert "1 of" in cmp.check("duty per step").detail
    # the motion itself is untouched
    for name in ("step count", "step deltas", "cumulative position (time aligned)", "path deviation (spatial)",
                 "process items (sequence and values)"):
        assert cmp.check(name).status == C.PASS, name
    assert cmp.first_divergence["item"] == wi


def test_swapped_process_items_detected_and_located(setup):
    wire = _wire(setup)
    proc = [i for i, w in enumerate(wire) if (w[0] & 0xFFFF) != F.OP_STEP]
    a, b = next((x, y) for x, y in zip(proc, proc[1:]) if y == x + 1 and wire[x] != wire[y])
    res = _run(setup, S.edit_wire(setup["msgs"], swap=(a, b)), "swap.pcap")
    cmp = res.comparison
    assert cmp.verdict.startswith("DIFFERENT") and "process items" in cmp.verdict
    assert cmp.check("process items (sequence and values)").status == C.FAIL
    n_a = proc.index(a)
    assert cmp.info["process_first_index"] == n_a
    assert cmp.process_diff                    # difflib blocks for the report
    assert f"#{n_a}" in cmp.check("process items (sequence and values)").detail
    assert cmp.first_divergence["item"] == a
    for name in ("step count", "step deltas", "duty per step", "frequency per step", "power per step"):
        assert cmp.check(name).status == C.PASS, name


def test_changed_frequency_on_laser_off_step_is_ignored_by_core_checks(setup):
    wire = _wire(setup)
    ss = C.step_stream(wire)
    k = int(next(i for i in range(ss.n) if not ss.duty[i]))
    res = _run(setup, S.edit_wire(setup["msgs"], set_step=(int(ss.step_wire_index[k]), {"freq": 1234})), "f.pcap")
    cmp = res.comparison
    assert cmp.check("frequency per step").status == C.PASS        # laser-off steps do not count
    assert "1 incl. laser-off" in cmp.check("frequency per step").detail
    assert cmp.verdict.startswith("EQUIVALENT")


# ----------------------------------------------------------------------------- CLI
def test_cli_match_html_json(setup, capsys):
    tool = _tool()
    pc = _write(setup, setup["msgs"], "cli.pcap")
    html, js = setup["dir"] / "r.html", setup["dir"] / "r.json"
    rc = tool.main([pc, setup["dxf"], "--params", setup["dat"], "--html", str(html), "--json", str(js)])
    out = capsys.readouterr().out
    assert rc == 0 and "VERDICT: IDENTICAL" in out
    h = html.read_text(encoding="utf-8")
    assert "IDENTICAL" in h and "<title>" in h
    pytest.importorskip("matplotlib")
    assert h.count("data:image/png;base64,") >= 4
    d = json.loads(js.read_text())
    assert d["verdict"].startswith("IDENTICAL") and d["info"]["capture_steps"] == setup["job"].steps


def test_cli_difference_exit_code_and_list(setup, capsys):
    tool = _tool()
    wire = _wire(setup)
    ss = C.step_stream(wire)
    pc = _write(setup, S.edit_wire(setup["msgs"], drop=int(ss.step_wire_index[ss.n // 2])), "cli_drop.pcap")
    html = setup["dir"] / "d.html"
    assert tool.main([pc, setup["dxf"], "--params", setup["dat"], "--html", str(html), "-q"]) == 1
    assert "DIFFERENT" in capsys.readouterr().out
    pytest.importorskip("matplotlib")
    assert html.read_text(encoding="utf-8").count("data:image/png;base64,") == 5   # incl. the zoom
    dump = setup["dir"] / "dump.txt"
    assert tool.main([pc, "--list", "--dump", str(dump)]) == 0
    out = capsys.readouterr().out
    assert "1 job(s)" in out and "job 0:" in out
    assert "step dx=" in dump.read_text()
    assert tool.main([str(setup["dir"] / "missing.pcap"), setup["dxf"]]) == 2
