"""One mlaser per user session (UI evaluation F6, mlaser/ui/single_instance.py): a second
launch hands its file to the running instance and exits; the instance lock dies with its
process; another protocol version is refused with a clear message."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("PySide6")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(ROOT, "tests", "single_instance_driver.py")
DXF = os.path.join(ROOT, "tests", "data", "dxf", "mix.dxf")


@pytest.fixture
def env(tmp_path):
    """An isolated user session: config, data and runtime directories of its own."""
    e = dict(os.environ)
    for k in ("PYTEST_CURRENT_TEST", "PYTEST_XDIST_WORKER", "MLASER_SINGLE_INSTANCE"):
        e.pop(k, None)
    for name in ("CONFIG", "DATA", "APP", "LOG"):
        d = tmp_path / name.lower()
        d.mkdir()
        e[f"MLASER_{name}_DIR"] = str(d)
    rt = tmp_path / "rt"
    e.update(MLASER_RUNTIME_DIR=str(rt), QT_QPA_PLATFORM="offscreen", MLASER_NO_FIRSTRUN="1",
             MLASER_NO_INHIBIT="1", MLASER_PLAN_PROCESS="0", PYTHONPATH=ROOT)
    return e


def _start_first(env, report, crash=False):
    e = dict(env, MLASER_DRIVER_SECONDS="90", **({"MLASER_DRIVER_CRASH": "1"} if crash else {}))
    p = subprocess.Popen([sys.executable, DRIVER, str(report), "--fake"], env=e, cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    t_end = time.monotonic() + 90
    while time.monotonic() < t_end:
        st = _report(report)
        if st.get("ready"):
            return p
        if p.poll() is not None:
            raise AssertionError(f"first instance ended: {p.stderr.read().decode(errors='replace')[-2000:]}")
        time.sleep(0.1)
    p.kill()
    raise AssertionError("first instance did not come up")


def _report(path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _stop(p, report) -> int:
    open(str(report) + ".quit", "w").close()
    try:
        return p.wait(30)
    except subprocess.TimeoutExpired:
        p.kill()
        raise


def _launch(env, *args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "mlaser.ui", "--fake", *args], env=env, cwd=ROOT,
                          capture_output=True, timeout=timeout)


def test_second_launch_hands_its_file_over_and_exits(env, tmp_path):
    report = tmp_path / "first.json"
    first = _start_first(env, report)
    try:
        t0 = time.monotonic()
        r = _launch(env, DXF)
        dt = time.monotonic() - t0
        assert r.returncode == 0, r.stderr.decode(errors="replace")[-2000:]
        assert b"already running" in r.stderr
        t_end = time.monotonic() + 30
        st = _report(report)
        while time.monotonic() < t_end and os.path.abspath(DXF) not in st.get("recent", []):
            time.sleep(0.1)
            st = _report(report)
        assert st["opened"] == [os.path.abspath(DXF)], st
        assert st["recent"][:1] == [os.path.abspath(DXF)] and st["requests"] == 1, st
        assert dt < 30, f"the hand-over took {dt:.1f} s"
        # no file: the running window just comes to the front
        r = _launch(env)
        assert r.returncode == 0 and b"brought to the front" in r.stderr
        t_end = time.monotonic() + 10
        while time.monotonic() < t_end and _report(report).get("requests") != 2:
            time.sleep(0.1)
        assert _report(report)["requests"] == 2 and _report(report)["opened"] == [os.path.abspath(DXF)]
    finally:
        assert _stop(first, report) == 0


def test_the_instance_lock_dies_with_a_crashed_process(env, tmp_path):
    report = tmp_path / "first.json"
    first = _start_first(env, report, crash=True)
    assert _stop(first, report) == 3                     # terminated: no clean-up ran
    # its lock file stays (the OS dropped the lock itself), and so may its socket (in the
    # runtime directory, or in Qt's temp directory when that path is too long for a socket)
    locks = [f for f in os.listdir(env["MLASER_RUNTIME_DIR"]) if f.endswith(".lock")]
    assert locks
    # the OS releases the dead process' lock (Windows: soon after the process ended, not
    # necessarily at once: LockFileEx), and nothing else is needed for the next launch
    from mlaser._oslock import FileLock, LockHeld
    lock = FileLock(os.path.join(env["MLASER_RUNTIME_DIR"], locks[0]))
    t_end = time.monotonic() + 30
    while True:
        try:
            lock.acquire()
            break
        except LockHeld:
            assert time.monotonic() < t_end, "the crashed instance's lock was never released"
            time.sleep(0.1)
    lock.release()
    # the next launch is the first instance again (it does not try to hand over to the dead one)
    report2 = tmp_path / "second.json"
    second = _start_first(env, report2)
    try:
        r = _launch(env, DXF)
        assert r.returncode == 0, r.stderr.decode(errors="replace")[-2000:]
    finally:
        assert _stop(second, report2) == 0


def test_new_instance_starts_anyway(env, tmp_path):
    report = tmp_path / "first.json"
    first = _start_first(env, report)
    try:
        r = subprocess.run([sys.executable, "-m", "mlaser", "--smoke-test", "1", "--new-instance", "--fake"],
                           env=env, cwd=ROOT, capture_output=True, timeout=90)
        assert r.returncode == 0, r.stderr.decode(errors="replace")[-2000:]
        assert b"GUI ran for 1 s" in r.stderr and b"already running" not in r.stderr
        assert _report(report)["requests"] == 0
    finally:
        _stop(first, report)


def test_enabled_switch():
    from mlaser.ui import single_instance as SI
    assert SI.enabled({}) and SI.enabled({"MLASER_SINGLE_INSTANCE": "1"})
    assert not SI.enabled({"MLASER_SINGLE_INSTANCE": "0"})
    assert SI.instance_key("/a/b") == SI.instance_key("/a/b") != SI.instance_key("/a/c")


def test_server_refuses_another_protocol():
    from mlaser.ui import single_instance as SI
    inst = SI.SingleInstance("k" * 16)
    reply, files = inst._handle(json.dumps({"proto": SI.PROTO + 1, "cmd": "open", "files": ["x"]}).encode())
    assert not reply["ok"] and "protocol" in reply["error"] and files == []
    reply, files = inst._handle(b"not json")
    assert not reply["ok"] and files == []
    reply, files = inst._handle(json.dumps({"proto": SI.PROTO, "cmd": "open", "files": ["/a.dxf"]}).encode())
    assert reply["ok"] and files == ["/a.dxf"] and inst.requests == 1


FAKE_SERVER = """
import json, socket, sys
path, proto = sys.argv[1], int(sys.argv[2])
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(path)
srv.listen(1)
print("ready", flush=True)
c, _ = srv.accept()
c.sendall(json.dumps({"app": "mlaser", "proto": proto, "pid": 1}).encode() + b"\\n")
c.settimeout(3)
try:
    data = c.recv(4096)
except OSError:
    data = b""
print("got", len(data), flush=True)
"""


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix domain sockets")
def test_client_refuses_a_server_of_another_protocol(tmp_path):
    # the server runs in a process of its own: QLocalSocket's blocking waits hold the GIL
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from mlaser.ui import single_instance as SI
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix="mli-", dir="/tmp" if os.path.isdir("/tmp") else None), "s.sock")
    p = subprocess.Popen([sys.executable, "-c", FAKE_SERVER, path, str(SI.PROTO + 1)],
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        inst = SI.SingleInstance("p" * 16)
        inst.name = path
        with pytest.raises(SI.InstanceError, match="protocol"):
            inst.forward(["x.dxf"], timeout_s=5)
        assert p.stdout.readline().strip() == "got 0"          # nothing sent after the hello
    finally:
        p.kill()
        p.wait(5)
