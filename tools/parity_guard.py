#!/usr/bin/env python3
"""Parity guard: a pull request that changes the goldens or the parity fence must say so.

``tests/golden/`` (the frozen artefacts of ``tools/make_goldens.py``) and
``mlaser/parity_ports/`` (the bit-exact ports of the original) change only on purpose, after a
reviewed behaviour change (CONTRIBUTING.md, "Parity and bit-exact rules").  This check fails a
pull request that touches either of them unless

* the pull request carries the label ``parity-change``, or
* a commit of the pull request has a ``Parity-Change:`` trailer with a reason, e.g.::

      Parity-Change: kerf corner fix of #123 changes 4 golden jobs; Wine re-run 2026-11-02

Usage (CI, .github/workflows/parity-guard.yml)::

    python tools/parity_guard.py --base <base sha> --head <head sha> [--labels '["a", "b"]']

``--labels`` takes a JSON list or a comma-separated list (``$PARITY_GUARD_LABELS`` when the
option is not given).  Exit code 0: allowed (nothing protected changed, or declared);
1: protected paths changed without a declaration; 2: git failed.  Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Iterable, Optional, Sequence

PROTECTED = ("tests/golden/", "mlaser/parity_ports/")
LABEL = "parity-change"
TRAILER = "Parity-Change"

_TRAILER_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)\s*:\s*(.*)$")


def protected_changes(paths: Iterable[str], protected: Sequence[str] = PROTECTED) -> list[str]:
    """The changed paths below a protected prefix (sorted, '/' separators)."""
    out = set()
    for p in paths:
        q = p.strip().replace("\\", "/")
        if q and any(q.startswith(pre) for pre in protected):
            out.add(q)
    return sorted(out)


def trailers(message: str) -> dict[str, list[str]]:
    """The trailers of a commit message: ``Key: value`` lines of its last paragraph, as
    ``git interpret-trailers --parse`` reads them (every line of the block must be a trailer
    or a continuation line starting with whitespace)."""
    paras = [p for p in re.split(r"\n\s*\n", message.strip()) if p.strip()]
    if len(paras) < 2:            # a subject line alone has no trailer block
        return {}
    out: dict[str, list[str]] = {}
    last: Optional[str] = None
    for line in paras[-1].splitlines():
        if line[:1] in (" ", "\t") and last is not None:
            out[last][-1] = (out[last][-1] + " " + line.strip()).strip()
            continue
        m = _TRAILER_LINE.match(line.strip())
        if not m:
            return {}             # not a trailer block
        last = m.group(1).lower()
        out.setdefault(last, []).append(m.group(2).strip())
    return out


def declared_in_messages(messages: Iterable[str]) -> list[str]:
    """The reasons of every non-empty ``Parity-Change:`` trailer in the messages."""
    key = TRAILER.lower()
    return [v for msg in messages for v in trailers(msg).get(key, []) if v]


def parse_labels(raw: Optional[str]) -> list[str]:
    if not raw or not raw.strip():
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    return [x.strip() for x in raw.split(",") if x.strip()]


def decide(changed: Iterable[str], labels: Iterable[str], messages: Iterable[str]) -> tuple[bool, str]:
    """(allowed, explanation) for one pull request."""
    hit = protected_changes(changed)
    if not hit:
        return True, "no change below " + ", ".join(PROTECTED)
    head = f"{len(hit)} protected file(s) changed:\n" + "\n".join(f"  {p}" for p in hit[:50])
    if len(hit) > 50:
        head += f"\n  ... and {len(hit) - 50} more"
    if LABEL in set(labels):
        return True, head + f"\nallowed: the pull request has the label '{LABEL}'"
    reasons = declared_in_messages(messages)
    if reasons:
        return True, head + f"\nallowed: {TRAILER}: " + " | ".join(reasons)
    return False, (head + "\n\nThe goldens (tests/golden/) and the parity fence (mlaser/parity_ports/) change only "
                   "after a reviewed behaviour change.  If this change is intended, add the label "
                   f"'{LABEL}' to the pull request, or a trailer to one of its commits:\n\n"
                   f"  {TRAILER}: <why the frozen output changes; Wine re-run or review reference>\n\n"
                   "See CONTRIBUTING.md, 'Parity and bit-exact rules'.")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def git_changed(base: str, head: str) -> list[str]:
    return [p for p in _git("diff", "--name-only", "--no-renames", f"{base}...{head}").splitlines() if p]


def git_messages(base: str, head: str) -> list[str]:
    out = _git("log", "--format=%B%x1e", f"{base}..{head}")
    return [m.strip() for m in out.split("\x1e") if m.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="base commit of the pull request")
    ap.add_argument("--head", required=True, help="head commit of the pull request")
    ap.add_argument("--labels", default=None, help="JSON list or comma-separated label names")
    a = ap.parse_args(argv)
    labels = parse_labels(a.labels if a.labels is not None else os.environ.get("PARITY_GUARD_LABELS"))
    try:
        changed = git_changed(a.base, a.head)
        messages = git_messages(a.base, a.head)
    except (OSError, subprocess.CalledProcessError) as exc:
        err = getattr(exc, "stderr", "") or exc
        print(f"parity guard: git failed: {err}", file=sys.stderr)
        return 2
    ok, why = decide(changed, labels, messages)
    print(why)
    if not ok:
        print(f"::error title=Parity guard::protected files changed without the '{LABEL}' label "
              f"or a '{TRAILER}:' trailer (see the log)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
