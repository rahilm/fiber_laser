"""PEP 440 versions, version specifiers, PEP 508 requirements and environment markers.

A small, dependency-free subset of what the ``packaging`` library does, enough for the plugin
standard (docs/dev/PLUGIN_STANDARD.md): the manifest's ``api`` / ``mlaser`` ranges, the index's
versions and a wheel's ``Requires-Dist``.  The application does not depend on ``packaging``
(the frozen bundle does not ship it), so the rules are implemented here and tested against
``packaging`` where it is installed (tests/test_plugin_manager.py).

Supported: epochs, release segments, ``a`` / ``b`` / ``rc`` pre-releases (and their alternative
spellings), post and dev releases, local versions; the operators ``~=``, ``==`` (with ``.*``),
``!=`` (with ``.*``), ``<=``, ``>=``, ``<``, ``>``, ``===``; markers with ``and`` / ``or`` /
parentheses over the standard variables.

Pre-releases: :meth:`SpecifierSet.contains` takes ``prereleases``.  The plugin manager checks
the *running* application and plugin API with ``prereleases=True`` (it is a compatibility test of
what is installed, not a selection), and selects plugin versions from an index with pip's rule
(no pre-release unless asked for or named by the specifier).
"""
from __future__ import annotations

import os
import platform
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

__all__ = ["InvalidVersion", "InvalidSpecifier", "InvalidRequirement", "Version", "SpecifierSet",
           "Requirement", "canonical_name", "evaluate_marker", "default_environment"]


class InvalidVersion(ValueError):
    pass


class InvalidSpecifier(ValueError):
    pass


class InvalidRequirement(ValueError):
    pass


_VERSION_RE = re.compile(
    r"""^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?P<post>(?:-(?P<post_n1>[0-9]+))|(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?))?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$""", re.VERBOSE | re.IGNORECASE)
_PRE_NAMES = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "c": "rc", "rc": "rc", "pre": "rc",
              "preview": "rc"}
_INF = (float("inf"),)
_NINF = (float("-inf"),)


@dataclass(frozen=True)
class Version:
    epoch: int
    release: tuple[int, ...]
    pre: Optional[tuple[str, int]] = None
    post: Optional[int] = None
    dev: Optional[int] = None
    local: tuple = ()
    _key: tuple = field(default=(), compare=False, repr=False)

    @classmethod
    def parse(cls, text: str) -> "Version":
        if not isinstance(text, str) or len(text) > 128:
            raise InvalidVersion(f"invalid version {text!r}")
        m = _VERSION_RE.match(text)
        if not m:
            raise InvalidVersion(f"invalid version {text!r}")
        pre = None
        if m.group("pre_l"):
            pre = (_PRE_NAMES[m.group("pre_l").lower()], int(m.group("pre_n") or 0))
        post = None
        if m.group("post"):
            post = int(m.group("post_n1") or m.group("post_n2") or 0)
        dev = int(m.group("dev_n") or 0) if m.group("dev") else None
        local: tuple = ()
        if m.group("local"):
            local = tuple(int(p) if p.isdigit() else p.lower() for p in re.split(r"[-_.]", m.group("local")))
        release = tuple(int(p) for p in m.group("release").split("."))
        v = cls(int(m.group("epoch") or 0), release, pre, post, dev, local)
        object.__setattr__(v, "_key", v._cmpkey())
        return v

    def _cmpkey(self) -> tuple:
        rel = list(self.release)
        while len(rel) > 1 and rel[-1] == 0:
            rel.pop()
        if self.pre is None and self.post is None and self.dev is not None:
            pre: tuple = _NINF                       # 1.0.dev0 sorts before 1.0a0
        elif self.pre is None:
            pre = _INF
        else:
            pre = ({"a": 0, "b": 1, "rc": 2}[self.pre[0]], self.pre[1])
        post = _NINF if self.post is None else (self.post,)
        dev = _INF if self.dev is None else (self.dev,)
        local = () if not self.local else tuple((i, "") if isinstance(i, int) else (-1, i) for i in self.local)
        return (self.epoch, tuple(rel), pre, post, dev, local)

    # ordering
    def __lt__(self, other: "Version") -> bool:
        return self._key < other._key

    def __le__(self, other: "Version") -> bool:
        return self._key <= other._key

    def __gt__(self, other: "Version") -> bool:
        return self._key > other._key

    def __ge__(self, other: "Version") -> bool:
        return self._key >= other._key

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key == other._key

    def __hash__(self) -> int:
        return hash(self._key)

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    @property
    def public(self) -> "Version":
        return Version.parse(str(self).split("+", 1)[0])

    @property
    def base(self) -> tuple[int, int, tuple[int, ...]]:
        return (self.epoch, 0, self.release)

    def __str__(self) -> str:
        s = (f"{self.epoch}!" if self.epoch else "") + ".".join(map(str, self.release))
        if self.pre:
            s += f"{self.pre[0]}{self.pre[1]}"
        if self.post is not None:
            s += f".post{self.post}"
        if self.dev is not None:
            s += f".dev{self.dev}"
        if self.local:
            s += "+" + ".".join(map(str, self.local))
        return s


def _pad(a: tuple[int, ...], n: int) -> tuple[int, ...]:
    return a + (0,) * max(0, n - len(a))


_SPEC_RE = re.compile(r"^\s*(~=|===|==|!=|<=|>=|<|>)\s*([^\s,;]+)\s*$")


@dataclass(frozen=True)
class Specifier:
    op: str
    text: str

    def __str__(self) -> str:
        return f"{self.op}{self.text}"

    @property
    def mentions_prerelease(self) -> bool:
        try:
            return Version.parse(self.text.rstrip(".*").rstrip(".")).is_prerelease
        except InvalidVersion:
            return False

    def contains(self, v: Version) -> bool:
        op, text = self.op, self.text
        if op == "===":
            return str(v).lower() == text.lower()
        if op in ("==", "!=") and text.endswith(".*"):
            prefix = Version.parse(text[:-2])
            vv = v.public
            n = len(prefix.release)
            same = (vv.epoch == prefix.epoch and _pad(vv.release, n)[:n] == prefix.release
                    and (prefix.pre is None or vv.pre == prefix.pre))
            return same if op == "==" else not same
        spec = Version.parse(text)
        if op == "==":
            cand = v if spec.local else v.public
            return cand == spec
        if op == "!=":
            cand = v if spec.local else v.public
            return cand != spec
        vp = v.public
        if op == "~=":
            if len(spec.release) < 2:
                raise InvalidSpecifier(f"~= needs at least two release segments: {self}")
            n = len(spec.release) - 1
            return vp >= spec and spec.epoch == vp.epoch and _pad(vp.release, n)[:n] == spec.release[:n]
        if op == ">=":
            return vp >= spec
        if op == "<=":
            return vp <= spec
        if op == "<":
            if not vp < spec:
                return False
            # <V excludes the pre-releases of V itself unless V is one
            if not spec.is_prerelease and vp.is_prerelease and _pad(vp.release, len(spec.release)) == \
                    _pad(spec.release, len(vp.release)) and vp.epoch == spec.epoch:
                return False
            return True
        if op == ">":
            if not vp > spec:
                return False
            # >V excludes the post-releases of V unless V is one
            if spec.post is None and vp.post is not None and _pad(vp.release, len(spec.release)) == \
                    _pad(spec.release, len(vp.release)) and vp.epoch == spec.epoch:
                return False
            return not (v.local and v.public == spec)
        raise InvalidSpecifier(f"unknown operator {op!r}")


class SpecifierSet:
    """A comma-separated set of specifiers (``">=2,<3"``); empty matches everything."""

    def __init__(self, text: str = "") -> None:
        if not isinstance(text, str) or len(text) > 512:
            raise InvalidSpecifier(f"invalid specifier {text!r}")
        self.text = text.strip()
        self.specs: list[Specifier] = []
        for part in filter(None, (p.strip() for p in self.text.split(","))):
            m = _SPEC_RE.match(part)
            if not m:
                raise InvalidSpecifier(f"invalid specifier {part!r} in {text!r}")
            op, ver = m.group(1), m.group(2)
            if op != "===":
                try:
                    if ver.endswith(".*"):
                        if op not in ("==", "!="):
                            raise InvalidSpecifier(f"{op} does not take a wildcard: {part!r}")
                        Version.parse(ver[:-2])
                    else:
                        Version.parse(ver)
                except InvalidVersion as exc:
                    raise InvalidSpecifier(f"invalid version in {part!r}") from exc
            if op == "~=" and len(Version.parse(ver).release) < 2:
                raise InvalidSpecifier(f"~= needs at least two release segments: {part!r}")
            self.specs.append(Specifier(op, ver))

    def __str__(self) -> str:
        return ",".join(str(s) for s in self.specs)

    def __bool__(self) -> bool:
        return bool(self.specs)

    def contains(self, version: "Version | str", prereleases: Optional[bool] = None) -> bool:
        v = version if isinstance(version, Version) else Version.parse(str(version))
        if prereleases is None:
            prereleases = any(s.mentions_prerelease for s in self.specs)
        if v.is_prerelease and not prereleases:
            return False
        return all(s.contains(v) for s in self.specs)

    def __contains__(self, version: "Version | str") -> bool:
        return self.contains(version)


def canonical_name(name: str) -> str:
    """PEP 503 normalised project name (``Mlaser_Hello`` -> ``mlaser-hello``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------- markers
_MARKER_VARS = ("python_version", "python_full_version", "os_name", "sys_platform", "platform_release",
                "platform_system", "platform_version", "platform_machine", "platform_python_implementation",
                "implementation_name", "implementation_version", "extra")
_VERSION_VARS = {"python_version", "python_full_version", "implementation_version", "platform_release"}
_TOKEN_RE = re.compile(r"""\s*(?:(?P<str>'[^']*'|"[^"]*")|(?P<op>===|==|!=|<=|>=|~=|<|>|not\s+in\b|in\b)
                       |(?P<kw>and\b|or\b)|(?P<lp>\()|(?P<rp>\))|(?P<var>[A-Za-z_][A-Za-z0-9_.]*))""",
                       re.VERBOSE)


def default_environment() -> dict[str, str]:
    impl = sys.implementation
    iv = impl.version
    impl_ver = f"{iv.major}.{iv.minor}.{iv.micro}"
    if iv.releaselevel != "final":
        impl_ver += iv.releaselevel[0] + str(iv.serial)
    return {
        "implementation_name": impl.name, "implementation_version": impl_ver, "os_name": os.name,
        "platform_machine": platform.machine(), "platform_release": platform.release(),
        "platform_system": platform.system(), "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "platform_python_implementation": platform.python_implementation(),
        "python_version": ".".join(platform.python_version_tuple()[:2]), "sys_platform": sys.platform,
        "extra": "",
    }


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise InvalidRequirement(f"invalid marker {text!r} at {pos}")
        kind = m.lastgroup or ""
        val = m.group(kind)
        out.append((kind, re.sub(r"\s+", " ", val) if kind == "op" else val))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return out


def evaluate_marker(text: str, env: Optional[dict[str, str]] = None) -> bool:
    """Evaluate a PEP 508 environment marker (``python_version >= "3.12" and os_name == "nt"``)."""
    environment = default_environment()
    environment.update(env or {})
    toks = _tokens(text)
    pos = 0

    def peek() -> tuple[str, str]:
        return toks[pos] if pos < len(toks) else ("", "")

    def take(kind: str = "") -> tuple[str, str]:
        nonlocal pos
        t = peek()
        if not t[0] or (kind and t[0] != kind):
            raise InvalidRequirement(f"invalid marker {text!r}")
        pos += 1
        return t

    def value() -> tuple[str, str]:
        kind, v = take()
        if kind == "str":
            return ("str", v[1:-1])
        if kind == "var":
            if v not in _MARKER_VARS:
                raise InvalidRequirement(f"unknown marker variable {v!r}")
            return (v, environment.get(v, ""))
        raise InvalidRequirement(f"invalid marker {text!r}")

    def compare(lhs: tuple[str, str], op: str, rhs: tuple[str, str]) -> bool:
        a, b = lhs[1], rhs[1]
        if op == "in":
            return a in b
        if op == "not in":
            return a not in b
        if lhs[0] == "extra" or rhs[0] == "extra":
            a, b = canonical_name(a), canonical_name(b)
        if lhs[0] in _VERSION_VARS or rhs[0] in _VERSION_VARS:
            try:
                return SpecifierSet(f"{op}{b}").contains(a, prereleases=True)
            except (InvalidSpecifier, InvalidVersion):
                pass
        ops: dict[str, Callable[[str, str], bool]] = {
            "==": lambda x, y: x == y, "===": lambda x, y: x == y, "!=": lambda x, y: x != y,
            "<": lambda x, y: x < y, "<=": lambda x, y: x <= y, ">": lambda x, y: x > y,
            ">=": lambda x, y: x >= y}
        if op not in ops:
            raise InvalidRequirement(f"operator {op!r} needs versions in {text!r}")
        return ops[op](a, b)

    def atom() -> bool:
        if peek()[0] == "lp":
            take("lp")
            r = expr()
            take("rp")
            return r
        lhs = value()
        _, op = take("op")
        rhs = value()
        return compare(lhs, op, rhs)

    def conj() -> bool:
        r = atom()
        while peek() == ("kw", "and"):
            take()
            r2 = atom()
            r = r and r2
        return r

    def expr() -> bool:
        r = conj()
        while peek() == ("kw", "or"):
            take()
            r2 = conj()
            r = r or r2
        return r

    result = expr()
    if pos != len(toks):
        raise InvalidRequirement(f"invalid marker {text!r}")
    return result


_REQ_RE = re.compile(r"""^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*
                      (?:\[(?P<extras>[^\]]*)\])?\s*
                      (?P<url>@\s*\S+)?\s*
                      (?:\(?(?P<spec>[^;()@]*)\)?)?\s*
                      (?:;\s*(?P<marker>.+))?$""", re.VERBOSE)


@dataclass(frozen=True)
class Requirement:
    name: str
    extras: tuple[str, ...]
    specifier: SpecifierSet
    marker: str = ""
    url: str = ""
    text: str = ""

    @classmethod
    def parse(cls, text: str) -> "Requirement":
        if not isinstance(text, str) or len(text) > 1024:
            raise InvalidRequirement(f"invalid requirement {text!r}")
        m = _REQ_RE.match(text)
        if not m:
            raise InvalidRequirement(f"invalid requirement {text!r}")
        try:
            spec = SpecifierSet((m.group("spec") or "").strip())
        except InvalidSpecifier as exc:
            raise InvalidRequirement(f"invalid requirement {text!r}: {exc}") from exc
        extras = tuple(e.strip() for e in (m.group("extras") or "").split(",") if e.strip())
        marker = (m.group("marker") or "").strip()
        if marker:
            evaluate_marker(marker)          # syntax check
        return cls(m.group("name"), extras, spec, marker, (m.group("url") or "").lstrip("@ ").strip(),
                   text.strip())

    def applies(self, env: Optional[dict[str, Any]] = None) -> bool:
        """Does the marker select this requirement here (no ``extra`` is requested)?"""
        return not self.marker or evaluate_marker(self.marker, env)

    @property
    def key(self) -> str:
        return canonical_name(self.name)
