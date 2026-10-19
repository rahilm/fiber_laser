"""Fuzzy command search for the command palette (Ctrl+K).  Qt-free.

:func:`score` rates how well a query matches a text: every query character must occur in order
(a subsequence, case-insensitive); consecutive runs, matches at word starts and a match at the
very start score higher, gaps cost a little.  :func:`search` ranks palette entries by the best
score over their label, id and keywords, with recently used commands first (most recent first
among equal matches, and the recent list alone for an empty query).

:class:`RecentCommands` keeps the most-recently-used list (persisted by the shell in the
ConfigService as ``shell.recent_commands``).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class Entry:
    id: str
    label: str
    workspace: str = ""
    shortcut: str = ""
    keywords: tuple[str, ...] = ()
    enabled: bool = True


@dataclass
class Match:
    entry: Entry
    score: float
    positions: tuple[int, ...] = ()  # matched character positions in entry.label (highlighting)
    recent: bool = False


def _fold(s: str) -> str:
    """Lower case without accents ("Écrou" matches "ecrou")."""
    n = unicodedata.normalize("NFKD", s)
    return "".join(c for c in n if not unicodedata.combining(c)).lower()


def _is_boundary(text: str, i: int) -> bool:
    if i == 0:
        return True
    prev, cur = text[i - 1], text[i]
    return (not prev.isalnum()) or (prev.islower() and cur.isupper())


def score(query: str, text: str) -> tuple[float, tuple[int, ...]]:
    """(score, matched positions in ``text``); score 0 when ``query`` (spaces ignored) is not a
    subsequence of ``text``.  The best alignment is found by dynamic programming (O(len(query)
    × len(text))): +1 per character, +2 at a word start, +1.5 for a consecutive match, -0.05
    per skipped character, +3 when the text starts with the query, +1.5 when it contains it."""
    q = _fold(query).replace(" ", "")
    if not q:
        return 1.0, ()
    t = _fold(text)
    same = len(t) == len(text)          # folding kept the positions (highlighting possible)
    src = text if same else t
    n, m = len(q), len(t)
    if n > m:
        return 0.0, ()
    NEG = float("-inf")
    bonus = [1.0 + (2.0 if _is_boundary(src, j) else 0.0) for j in range(m)]
    prev = [bonus[j] if t[j] == q[0] else NEG for j in range(m)]
    back: list[list[int]] = [[-1] * m]
    for i in range(1, n):
        cur = [NEG] * m
        bk = [-1] * m
        run_best, run_k = NEG, -1      # max over k < j-1 of prev[k] + 0.05 * (k + 1)
        for j in range(m):
            if j >= 2 and prev[j - 2] != NEG:
                v = prev[j - 2] + 0.05 * (j - 1)
                if v > run_best:
                    run_best, run_k = v, j - 2
            if t[j] != q[i]:
                continue
            best, k = NEG, -1
            if j >= 1 and prev[j - 1] != NEG:
                best, k = prev[j - 1] + 1.5, j - 1
            if run_best != NEG:
                v = run_best - 0.05 * j
                if v > best:
                    best, k = v, run_k
            if k >= 0:
                cur[j] = best + bonus[j]
                bk[j] = k
        back.append(bk)
        prev = cur
    j_end = max(range(m), key=lambda j: prev[j])
    best = prev[j_end]
    if best == NEG:
        return 0.0, ()
    pos = [j_end]
    for i in range(n - 1, 0, -1):
        pos.append(back[i][pos[-1]])
    pos.reverse()
    if t.startswith(q):
        best += 3.0
    elif q in t:
        best += 1.5
    best -= 0.01 * m
    return max(best, 0.01), (tuple(pos) if same else ())


def search(entries: Iterable[Entry], query: str, recent: Sequence[str] = (), limit: Optional[int] = None,
           include_disabled: bool = False) -> list[Match]:
    """Entries matching ``query``, best first.  Recently used commands rank first among similar
    matches; an empty query lists the recent commands, then the rest alphabetically."""
    rank_recent = {cid: i for i, cid in enumerate(recent)}
    out: list[Match] = []
    q = query.strip()
    for e in entries:
        if not e.enabled and not include_disabled:
            continue
        if not q:
            out.append(Match(e, 1.0, (), e.id in rank_recent))
            continue
        s_label, pos = score(q, e.label)
        s_id, _ = score(q, e.id)
        s_kw = max((score(q, k)[0] for k in e.keywords), default=0.0)
        s_ws = score(q, e.workspace)[0] * 0.5 if e.workspace else 0.0
        best = max(s_label, s_id * 0.9, s_kw * 0.8, s_ws)
        if best <= 0.0:
            continue
        r = e.id in rank_recent
        if r:
            best += 4.0 - 0.1 * min(rank_recent[e.id], 30)
        out.append(Match(e, best, pos if s_label >= best - (4.0 if r else 0.0) - 1e-9 else (), r))
    if not q:
        out.sort(key=lambda m: (not m.recent, rank_recent.get(m.entry.id, 0), m.entry.label.lower()))
    else:
        out.sort(key=lambda m: (-m.score, m.entry.label.lower()))
    return out[:limit] if limit else out


@dataclass
class RecentCommands:
    """Most recently used command ids, newest first."""

    ids: list[str] = field(default_factory=list)
    size: int = 12

    def add(self, cmd_id: str) -> None:
        if cmd_id in self.ids:
            self.ids.remove(cmd_id)
        self.ids.insert(0, cmd_id)
        del self.ids[self.size :]


__all__ = ["Entry", "Match", "score", "search", "RecentCommands"]
