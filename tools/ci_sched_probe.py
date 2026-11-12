"""How late a waiting thread runs while every CPU is busy (CI diagnostics, no bound).

The timing tests bound real-time behaviour in the hundreds of milliseconds (the 150 ms link
deadline, the streamer heartbeat, the GUI thread's longest pause).  A CI runner runs them next to
busy test workers on every CPU, and how long a thread that became ready waits for a CPU is then
the operating system's time slice: about 30 ms on a desktop Windows, about 180 ms on a Windows
Server with its default scheduling (the hosted runners), a few milliseconds on Linux and macOS.

This prints the measure for the machine it runs on: with ``cpu_count() + 2`` busy processes,

* the longest time a busy thread of this process stood still (a thread that lost its CPU waits
  for the others' time slices: the direct measure of the slice);
* how late a 5 ms ``time.sleep`` returns (a thread woken by a timer: a heartbeat, a poll);
* how long a UDP datagram takes to a thread of this process and back (a thread woken by a
  socket: the card simulator's reply).

    python tools/ci_sched_probe.py [seconds]
    python tools/ci_sched_probe.py --desktop-quanta [result file]    (Windows: see desktop_quanta)
"""
from __future__ import annotations

import multiprocessing as mp
import os
import socket
import sys
import threading
import time


def _spin(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


def _echo(sock: socket.socket) -> None:
    while True:
        try:
            data, addr = sock.recvfrom(64)
        except OSError:
            return
        if data == b"end":
            return
        sock.sendto(data, addr)


def measure(seconds: float = 3.0) -> dict:
    n = (os.cpu_count() or 1) + 2
    procs = [mp.Process(target=_spin, args=(seconds + 1.5,), daemon=True) for _ in range(n)]
    for p in procs:
        p.start()
    time.sleep(1.0)                                   # every spinner started (spawn on Windows)
    gap = 0.0
    last = time.perf_counter()
    end = last + seconds / 3
    while last < end:
        now = time.perf_counter()
        gap = max(gap, now - last)
        last = now
    late: list[float] = []
    end = time.monotonic() + seconds / 3
    while time.monotonic() < end:
        t0 = time.perf_counter()
        time.sleep(0.005)
        late.append(time.perf_counter() - t0 - 0.005)
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    th = threading.Thread(target=_echo, args=(srv,), daemon=True)
    th.start()
    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli.settimeout(2.0)
    rtt: list[float] = []
    end = time.monotonic() + seconds / 3
    while time.monotonic() < end:
        t0 = time.perf_counter()
        cli.sendto(b"x", srv.getsockname())
        try:
            cli.recvfrom(64)
        except OSError:
            rtt.append(2.0)
            continue
        rtt.append(time.perf_counter() - t0)
        time.sleep(0.002)
    cli.sendto(b"end", srv.getsockname())
    th.join(1.0)
    srv.close()
    cli.close()
    for p in procs:
        p.join(seconds + 5)
    return {"cpus": os.cpu_count(), "busy": n, "gap_worst": gap, "sleep_worst": max(late), "sleep_over_50ms": sum(x > 0.05 for x in late),
            "sleeps": len(late), "udp_worst": max(rtt), "udp_over_50ms": sum(x > 0.05 for x in rtt), "udps": len(rtt)}


def desktop_quanta() -> int:
    """Windows: give the running system the time slices of a desktop edition (short, variable:
    what "Adjust for best performance of: Programs" sets, Win32PrioritySeparation 0x26) instead
    of a server's long fixed ones.  Until the next boot only.  Returns the NTSTATUS (0: done)."""
    import ctypes
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)      # type: ignore[attr-defined]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)      # type: ignore[attr-defined]
    nt = ctypes.WinDLL("ntdll")                               # type: ignore[attr-defined]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    adv.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
                                          wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    tok = wintypes.HANDLE()
    if adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(tok)):
        for name in ("SeIncreaseBasePriorityPrivilege", "SeTcbPrivilege"):    # enabled when held
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            if adv.LookupPrivilegeValueW(None, name, ctypes.byref(tp.Privileges[0].Luid)):
                tp.Privileges[0].Attributes = 0x2
                adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
    value = ctypes.c_ulong(0x26)
    nt.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    nt.NtSetSystemInformation.restype = ctypes.c_long
    return nt.NtSetSystemInformation(39, ctypes.byref(value), 4) & 0xFFFFFFFF   # SystemPrioritySeparation


def main(argv: list[str]) -> int:
    if "--desktop-quanta" in argv:
        if sys.platform != "win32":
            print("desktop time slices: Windows only")
            return 0
        st = desktop_quanta()
        print(f"desktop time slices (Win32PrioritySeparation 0x26): status 0x{st:08X}" + ("" if st else ", done"))
        i = argv.index("--desktop-quanta")
        if len(argv) > i + 1:                          # (run as SYSTEM by CI: no console to read)
            with open(argv[i + 1], "w") as f:
                f.write(f"0x{st:08X}\n")
        return 1 if st else 0
    r = measure(float(argv[1]) if len(argv) > 1 else 4.5)
    print(f"scheduling probe: {r['cpus']} CPUs, {r['busy']} busy processes; "
          f"a busy thread stood still for at worst {r['gap_worst'] * 1000:.0f} ms; "
          f"a 5 ms sleep returned at worst {r['sleep_worst'] * 1000:.0f} ms late "
          f"({r['sleep_over_50ms']} of {r['sleeps']} over 50 ms); "
          f"a UDP echo inside the process took at worst {r['udp_worst'] * 1000:.0f} ms "
          f"({r['udp_over_50ms']} of {r['udps']} over 50 ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
