"""Compile-pool workers end when the program that started them dies (perf review 2026-11-09,
finding 11: the workers of crashed processes stayed for days, 83 of them on one machine)."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# a program that starts the compile pool, prints its workers' pids and dies without any clean-up
# (os._exit: no atexit, no pool shutdown - what a crash or a forced quit leaves)
CRASHER = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from mlaser.nc import parallel as P

    if __name__ == "__main__":
        pool = P._get_pool(2)
        futs = [pool.submit(os.getpid) for _ in range(8)]
        pids = sorted({f.result(timeout=120) for f in futs} | set(pool._processes))
        print(" ".join(str(p) for p in pids), flush=True)
        os._exit(3)
""")


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == 259                       # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():                                     # Linux: a zombie (not reaped) is dead
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except (OSError, IndexError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_pool_workers_end_when_their_parent_dies(tmp_path):
    script = tmp_path / "crasher.py"
    script.write_text(CRASHER)
    env = {**os.environ, "MLASER_COMPILE_WORKERS": "2"}
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    # files, not pipes: the workers inherit them, and a pipe would stay open while they live
    with open(out, "w") as fo, open(err, "w") as fe:
        r = subprocess.run([sys.executable, str(script), str(ROOT)], stdout=fo, stderr=fe,
                           stdin=subprocess.DEVNULL, timeout=180, env=env)
    assert r.returncode == 3, err.read_text()
    pids = [int(p) for p in out.read_text().split()]
    assert len(pids) >= 1
    deadline = time.monotonic() + 15.0
    left = pids
    while left and time.monotonic() < deadline:
        time.sleep(0.1)
        left = [p for p in left if _alive(p)]
    for p in left:                                         # leave nothing behind, then fail
        try:
            os.kill(p, 9)
        except OSError:
            pass
    assert not left, f"pool workers outlived their parent: {left}"


def test_a_live_parent_keeps_its_workers():
    """The watch does not end the workers of a healthy parent (here: this test process)."""
    from mlaser.nc import parallel as P
    pool = P._get_pool(2)
    try:
        first = {pool.submit(os.getpid).result(timeout=120) for _ in range(4)}
        time.sleep(2.5 * P.PARENT_WATCH_S)
        again = {pool.submit(os.getpid).result(timeout=120) for _ in range(4)}
        assert again <= set(pool._processes) and first & set(pool._processes)
    finally:
        P.shutdown()
