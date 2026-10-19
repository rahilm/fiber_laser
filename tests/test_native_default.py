"""The C emitter kernel as the default (mlaser.parity_ports.profiles.native): selection at first
use, the built-in self-check, the fallback to the Python port, ``MLASER_NATIVE`` and
:func:`configure`, and what the default profile builds.  docs/dev/NATIVE_KERNEL.md.

The goldens run under both modes in CI (``MLASER_NATIVE=1`` and ``=0``, every OS):
``tests/golden`` with the default profile, plus :func:`test_goldens_mode_matches_env` below.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import mlaser.parity_ports.profiles.native as N
from mlaser.parity_ports import ORIGINAL, ORIGINAL_PYTHON, PROFILES, get_profile, kernel_status
from mlaser.parity_ports.mainapp import sampling as S

needs_kernel = pytest.mark.skipif(N._K is None, reason="mlaser._native not built")
_ON = ("1", "on", "true", "yes", "require", "native")
_OFF = ("0", "off", "false", "no", "python")


@pytest.fixture
def fresh(monkeypatch):
    """A clean selection in mode auto (MLASER_NATIVE unset) for the test; the process's own
    selection is restored afterwards."""
    saved = (N._status, N._mode_override)
    N._status, N._mode_override = None, None
    monkeypatch.delenv(N.ENV, raising=False)
    yield monkeypatch
    monkeypatch.undo()
    N._status, N._mode_override = saved


def _machine():
    return SimpleNamespace(pulses_per_mm_x=1000.0, pulses_per_mm_y=1000.0, max_step=127, period_ms=1.0)


def _emitter():
    return ORIGINAL.sampling.Emitter(_machine(), (0.0, 0.0), False, (0.0, 0.0))


def test_profiles_registered():
    assert PROFILES["original-python"] is ORIGINAL_PYTHON
    assert get_profile("original-reference").sampling is ORIGINAL_PYTHON.sampling
    assert ORIGINAL_PYTHON.sampling.Emitter is S.Emitter
    assert ORIGINAL_PYTHON.sampling.emit_samples is S.emit_samples
    assert ORIGINAL.sampling.Emitter is N.DefaultEmitter
    assert ORIGINAL.sampling.emit_samples is N.emit_samples
    # the default profile's emit_samples hands a port Emitter to the port
    em = S.Emitter(_machine(), (0.0, 0.0), False)
    em.laser = True
    N.emit_samples(em, [(0.1, 0.0), (0.2, 0.05)], [0.1, 0.21])
    ref = S.Emitter(_machine(), (0.0, 0.0), False)
    ref.laser = True
    S.emit_samples(ref, [(0.1, 0.0), (0.2, 0.05)], [0.1, 0.21])
    assert em.wire == ref.wire and em.carry == ref.carry


def test_default_follows_the_environment():
    """What this process runs: native unless MLASER_NATIVE says otherwise (or nothing is built)."""
    st = kernel_status()
    env = __import__("os").environ.get(N.ENV, "").strip().lower()
    if env in _OFF:
        assert st.active == "python" and st.mode == "off"
    elif env in _ON:
        assert st.active == "native", st.text()  # CI forces it: a missing build fails here
    else:
        assert st.active == ("native" if N._K is not None else "python"), st.text()
    assert type(_emitter()) is (N.NativeEmitter if st.active == "native" else S.Emitter)
    assert st.text().startswith("Emitter kernel: ")
    assert set(st.to_dict()) >= {"active", "mode", "reason", "selfcheck", "module", "kernel_version"}


@pytest.mark.parametrize(
    "value, mode",
    [
        ("0", "off"),
        ("off", "off"),
        ("python", "off"),
        ("1", "on"),
        ("ON", "on"),
        ("", "auto"),
        ("auto", "auto"),
    ],
)
def test_env_values(fresh, value, mode):
    fresh.setenv(N.ENV, value)
    st = kernel_status()
    assert st.mode == mode
    if mode == "off":
        assert st.active == "python" and type(_emitter()) is S.Emitter
    elif N._K is not None:
        assert st.active == "native" and st.selfcheck == "passed"
        assert type(_emitter()) is N.NativeEmitter


def test_invalid_env_value_means_auto(fresh, caplog):
    fresh.setenv(N.ENV, "maybe")
    with caplog.at_level(logging.WARNING, logger=N.__name__):
        st = kernel_status()
    assert st.mode == "auto" and "invalid" in st.source
    assert any("maybe" in r.getMessage() for r in caplog.records)


def test_configure_overrides_the_environment(fresh):
    fresh.setenv(N.ENV, "1")
    assert N.configure("off").active == "python"
    assert type(_emitter()) is S.Emitter
    assert N.kernel_status().source == "configure()"
    with pytest.raises(ValueError):
        N.configure("sometimes")
    st = N.configure(None)  # back to MLASER_NATIVE=1
    assert st.mode == "on"


def test_missing_kernel_auto_falls_back_with_a_warning(fresh, caplog):
    fresh.setattr(N, "_K", None)
    fresh.delenv(N.ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger=N.__name__):
        st = kernel_status()
    assert st.active == "python" and "not built" in st.reason
    assert any("Python port" in r.getMessage() for r in caplog.records)
    assert type(_emitter()) is S.Emitter


def test_missing_kernel_required_raises(fresh):
    fresh.setattr(N, "_K", None)
    fresh.setenv(N.ENV, "1")
    assert kernel_status().active == "error"  # the status itself never raises
    with pytest.raises(N.NativeKernelMissing, match="not built"):
        _emitter()


def test_failed_selfcheck_auto_falls_back(fresh, caplog):
    if N._K is None:
        fresh.setattr(N, "_K", object())  # "importable" but never called
    fresh.setattr(N, "selfcheck", lambda: "output differs from the Python port in carry x")
    with caplog.at_level(logging.WARNING, logger=N.__name__):
        st = kernel_status()
    assert st.active == "python" and st.selfcheck.startswith("failed")
    assert "falling back" in st.reason
    assert any("self-check failed" in r.getMessage() for r in caplog.records)
    assert type(_emitter()) is S.Emitter


def test_failed_selfcheck_required_raises(fresh):
    if N._K is None:
        fresh.setattr(N, "_K", object())
    fresh.setattr(N, "selfcheck", lambda: "boom")
    fresh.setenv(N.ENV, "on")
    with pytest.raises(N.NativeSelfCheckFailed, match="boom"):
        _emitter()


def test_selfcheck_exception_counts_as_failure(fresh):
    if N._K is None:
        fresh.setattr(N, "_K", object())

    def boom():
        raise RuntimeError("kernel crashed")

    fresh.setattr(N, "selfcheck", boom)
    st = kernel_status()
    assert st.active == "python" and "kernel crashed" in st.reason


@needs_kernel
def test_selfcheck_passes_and_leaves_stats_alone():
    before = dict(N.STATS)
    assert N.selfcheck() == ""
    assert N.STATS == before


class _Skewed:
    """The real kernel, with one output perturbed (a miscompiled build)."""

    def __init__(self, k, field):
        self._k, self._field = k, field

    def __getattr__(self, name):
        return getattr(self._k, name)

    def emit(self, *a):
        st = self._k.emit(*a)
        if st is None:
            return st
        st = list(st)
        if self._field == "carry":
            st[2] = st[2] + 2.0**-40 if st[2] else 2.0**-60  # last-bits FMA-style error
        elif self._field == "on_len":
            st[8] = st[8] * (1.0 + 2.0**-52)
        return tuple(st)


@needs_kernel
@pytest.mark.parametrize("field", ["carry", "on_len"])
def test_selfcheck_detects_a_wrong_kernel(fresh, field):
    fresh.setattr(N, "_K", _Skewed(N._K, field))
    err = N.selfcheck()
    assert err and "differs" in err
    assert kernel_status().active == "python"


@needs_kernel
def test_selfcheck_detects_a_kernel_that_declines(fresh):
    class Declining(_Skewed):
        def emit(self, *a):
            return None

    fresh.setattr(N, "_K", Declining(N._K, ""))
    assert "declined" in N.selfcheck()


@needs_kernel
def test_native_sampling_profile_is_always_native(fresh):
    fresh.setenv(N.ENV, "0")
    assert kernel_status().active == "python"
    em = get_profile(N.NAME).sampling.Emitter(_machine(), (0.0, 0.0), False)
    assert type(em) is N.NativeEmitter


@pytest.mark.golden
def test_goldens_mode_matches_env():
    """Run with ``-m golden`` under MLASER_NATIVE=1 and =0 (CI): the goldens then pin both
    the kernel and the port; this records which one this run had."""
    test_default_follows_the_environment()
