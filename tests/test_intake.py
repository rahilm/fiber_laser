"""Job intake (mlaser.services.intake): submissions wait outside the queue until the operator
approves them; what a submission may carry; the inbox; approval and rejection."""
from __future__ import annotations

import json
import os
import threading

import pytest

from mlaser.services import intake as I
from mlaser.services.queue import JobQueue, QueueRunner


@pytest.fixture()
def prod(tmp_path):
    d = tmp_path / "production"
    d.mkdir()
    return d


@pytest.fixture()
def drawing(tmp_path):
    p = tmp_path / "jobs" / "flange.dxf"
    p.parent.mkdir()
    p.write_text("0\nEOF\n")
    return p


def audit_log():
    calls = []
    return calls, (lambda event, **kw: calls.append((event, kw)))


def test_a_submission_is_pending_never_queued(prod, drawing):
    calls, audit = audit_log()
    store = I.IntakeStore(prod, audit=audit)
    q = JobQueue(prod / "queue.json")
    job = store.submit({"path": str(drawing), "quantity": 2, "material": "S235", "thickness_mm": 3,
                        "note": "rush", "reference": "PO-1"}, "api", "10.0.0.7")
    assert store.pending == [] and len(list((prod / "inbox").glob("*.json"))) == 1
    assert store.scan() == [job] or [j.id for j in store.pending] == [job.id]
    assert [j.id for j in store.pending] == [job.id]
    assert len(q) == 0 and q.next_entry() is None
    # the pending list is its own file; queue.json never holds a pending job
    data = json.loads((prod / "pending.json").read_text("utf-8"))
    assert [d["id"] for d in data["pending"]] == [job.id]
    assert not (prod / "queue.json").exists()
    # the runner has nothing to run
    runner = QueueRunner(q, start=lambda: pytest.fail("started"))
    assert runner.begin() is False and runner.confirm_sheet() is False
    assert calls[0][0] == "intake_submitted" and calls[0][1]["client"] == "10.0.0.7"


def test_approval_makes_a_waiting_entry_that_still_needs_the_operator(prod, drawing):
    calls, audit = audit_log()
    store = I.IntakeStore(prod, audit=audit)
    q = JobQueue(prod / "queue.json")
    job = store.submit({"path": str(drawing), "quantity": 3, "name": "flange"}, "cli", "anna")
    store.scan()
    started = []
    runner = QueueRunner(q, start=lambda: started.append(1) or True)
    e = store.approve(job.id, q, operator="operator@machine")
    assert store.pending == [] and e.status == "waiting" and e.quantity == 3 and e.name == "flange"
    info = e.extra["intake"]
    assert info["source"] == "cli" and info["client"] == "anna" and info["approved_by"] == "operator@machine"
    assert "layers" not in e.extra                       # no cutting parameters from outside
    assert started == []                                 # approving never starts anything
    assert runner.begin() and runner.state == "confirm"   # "load the sheet, then confirm"
    assert started == []
    assert [c[0] for c in calls] == ["intake_submitted", "intake_approved"]
    # persisted: a restart sees the approved entry, not a pending one
    assert I.IntakeStore(prod, audit=audit).pending == []
    assert [x.id for x in JobQueue(prod / "queue.json")] == [e.id]


def test_reject_removes_it_and_an_upload(prod):
    store = I.IntakeStore(prod, audit=lambda *a, **k: None)
    path = store.store_upload("../../etc/evil name.DXF", b"0\nEOF\n")
    assert os.path.dirname(path) == str(prod / "inbox" / "files")
    assert os.path.basename(path).endswith("_evil name.dxf")        # directories dropped
    job = store.submit({"path": path}, "api")
    store.scan()
    store.reject(job.id, reason="wrong part")
    assert store.pending == [] and not os.path.exists(path)


@pytest.mark.parametrize("req, code", [
    ({"path": "x.dxf", "layers": {"1": {"power_pct": 100}}}, "unknown_field"),
    ({"path": "x.dxf", "extra": {}}, "unknown_field"),
    ({"path": "x.dxf", "technology": "S235"}, "unknown_field"),
    ({"name": "no path"}, "invalid"),
    ({"path": "x.nc"}, "unsupported"),
    ({"path": "x.mlz"}, "unsupported"),
    ({"path": "missing.dxf"}, "not_found"),
    ({"path": "x.dxf", "quantity": 0}, "invalid"),
    ({"path": "x.dxf", "quantity": 10000}, "invalid"),
    ({"path": "x.dxf", "quantity": True}, "invalid"),
    ({"path": "x.dxf", "quantity": "many"}, "invalid"),
    ({"path": "x.dxf", "thickness_mm": -1}, "invalid"),
    ({"path": "x.dxf", "thickness_mm": float("nan")}, "invalid"),
])
def test_validation(prod, req, code, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.dxf").write_text("0\nEOF\n")
    (tmp_path / "x.nc").write_text("G0")
    (tmp_path / "x.mlz").write_text("PK")
    with pytest.raises(I.IntakeError) as ei:
        I.validate(req, "api")
    assert ei.value.code == code


def test_text_fields_are_cleaned_and_capped(drawing):
    job = I.validate({"path": str(drawing), "name": "a\x00b\nc" + "x" * 500, "note": "n" * 2000}, "api", "c" * 999)
    assert "\x00" not in job.name and "\n" not in job.name and len(job.name) == 120
    assert len(job.note) == 500 and len(job.client) == 120
    assert job.name.startswith("abc")


def test_allowed_roots_and_symlink_escape(prod, drawing, tmp_path):
    outside = tmp_path / "secret.dxf"
    outside.write_text("0\nEOF\n")
    roots = [str(drawing.parent)]
    assert I.validate({"path": str(drawing)}, "api", allowed_roots=roots).path == os.path.realpath(drawing)
    for p in (str(outside), str(drawing.parent / ".." / "secret.dxf")):
        with pytest.raises(I.IntakeError) as ei:
            I.validate({"path": p}, "api", allowed_roots=roots)
        assert ei.value.code == "forbidden_path"
    link = drawing.parent / "link.dxf"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    with pytest.raises(I.IntakeError) as ei:
        I.validate({"path": str(link)}, "api", allowed_roots=roots)
    assert ei.value.code == "forbidden_path"
    with pytest.raises(I.IntakeError):
        I.validate({"path": str(drawing)}, "api", allowed_roots=[])     # no folder: nothing by path


def test_bad_inbox_files_are_set_aside(prod, drawing):
    store = I.IntakeStore(prod, audit=lambda *a, **k: None)
    inbox = prod / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "a.json").write_text("{not json")
    (inbox / "b.json").write_text(json.dumps({"format": "other", "job": {}}))
    (inbox / "c.json").write_text(json.dumps({"format": I.FORMAT, "job": {"id": "", "path": ""}}))
    (inbox / "d.json").write_bytes(b"x" * (I.MAX_REQUEST_BYTES + 1))
    good = store.submit({"path": str(drawing)}, "cli")
    new = store.scan()
    assert [j.id for j in new] == [good.id]
    assert sorted(p.name for p in inbox.glob("*.bad")) == ["a.json.bad", "b.json.bad", "c.json.bad", "d.json.bad"]
    assert len(store.errors) == 4
    # a damaged pending.json: the owner sets it aside, a CLI submitter never rewrites it
    (prod / "pending.json").write_text("{broken")
    I.IntakeStore(prod, owner=False, audit=lambda *a, **k: None)
    assert (prod / "pending.json").read_text() == "{broken"
    I.IntakeStore(prod, audit=lambda *a, **k: None)
    assert (prod / "pending.json.bad").exists()


def test_cap_counts_inbox_and_pending(prod, drawing):
    store = I.IntakeStore(prod, max_pending=3, audit=lambda *a, **k: None)
    store.submit({"path": str(drawing)}, "api")
    store.scan()
    store.submit({"path": str(drawing)}, "api")
    store.submit({"path": str(drawing)}, "api")
    with pytest.raises(I.IntakeError) as ei:
        store.submit({"path": str(drawing)}, "api")
    assert ei.value.code == "full" and store.count() == 3


def test_concurrent_submissions_from_threads(prod, drawing):
    store = I.IntakeStore(prod, max_pending=1000, audit=lambda *a, **k: None)
    errs = []

    def worker():
        try:
            for _ in range(20):
                store.submit({"path": str(drawing)}, "api")
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)
    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    assert len(store.scan()) == 100 and len({j.id for j in store.pending}) == 100


def test_approve_refuses_a_vanished_drawing(prod, drawing):
    store = I.IntakeStore(prod, audit=lambda *a, **k: None)
    job = store.submit({"path": str(drawing)}, "api")
    store.scan()
    drawing.unlink()
    q = JobQueue(None)
    with pytest.raises(I.IntakeError):
        store.approve(job.id, q)
    assert len(q) == 0 and [j.id for j in store.pending] == [job.id]


def test_public_view_hides_the_local_path(drawing):
    job = I.validate({"path": str(drawing)}, "api")
    pub = job.public()
    assert "path" not in pub and pub["file"] == "flange.dxf" and pub["status"] == "pending_approval"


def test_path_inside_across_windows_drives_and_case():
    """Windows CI: a drawing on D: and the job folders on C: made ``commonpath`` raise
    ValueError, which the API answered with 400 instead of the ``forbidden_path`` 403."""
    import ntpath
    import posixpath
    from mlaser.services.intake import path_inside
    assert not path_inside("D:\\a\\repo\\gear.dxf", "C:\\Users\\me\\jobs", ntpath)
    assert path_inside("C:\\Users\\Me\\Jobs\\gear.dxf", "c:\\users\\me\\jobs", ntpath)
    assert not path_inside("C:\\Users\\me\\jobs2\\gear.dxf", "C:\\Users\\me\\jobs", ntpath)
    assert path_inside("/srv/jobs/a/gear.dxf", "/srv/jobs", posixpath)
    assert not path_inside("/srv/jobs2/gear.dxf", "/srv/jobs", posixpath)
    assert not path_inside("/srv/Jobs/gear.dxf", "/srv/jobs", posixpath)   # POSIX: case matters
