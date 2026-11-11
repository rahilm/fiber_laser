"""A settings save written by a background thread (ConfigService.flush(background=True): the
counters after a job, a window closing) is read by the next load of the same file in this
process, even when the disk is slow: a second window opened right after the first saved its
counters gets them back (CI, ubuntu: test_diag_ui.test_job_statistics_end_to_end read 0)."""
import threading
import time

from mlaser.services import config as C


def test_load_waits_for_a_background_write(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    real = C.atomic_write_text
    started = threading.Event()

    def slow_write(p, text):
        started.set()
        time.sleep(0.5)                # a loaded runner's disk
        real(p, text)
    monkeypatch.setattr(C, "atomic_write_text", slow_write)
    a = C.ConfigService(path)
    a.set("SOP.TotalManuCount", 7)
    a.flush(background=True)
    assert started.wait(5)
    b = C.ConfigService(path)          # the second window, at once
    assert b.get("SOP.TotalManuCount") == 7
    assert C.wait_for_background_writes(path)
    assert not C._INFLIGHT


def test_wait_returns_at_once_with_no_writes(tmp_path):
    t0 = time.monotonic()
    assert C.wait_for_background_writes(tmp_path / "config.json")
    assert time.monotonic() - t0 < 1.0
