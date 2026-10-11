"""Minimal SemVer 2.0 parsing, precedence and range constraints (no external deps).

Supported constraint syntax (npm/helm-like subset):
    ">=1.2.0 <2.0.0"        AND (space or comma separated)
    "^1.4" | "~1.4.2"       caret / tilde ranges
    "1.x" | "1.2.*"         wildcards
    ">=2.0.0 || 1.9.x"      OR
Leading 'v' on versions is accepted ("v1.2.3").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    # '+' is not allowed in OCI tags, Helm stores build metadata with '_' instead.
    r"(?:[+_]([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple = ()
    build: str = ""

    @property
    def is_prerelease(self) -> bool:
        return bool(self.pre)

    def _key(self):
        # A version without prerelease has higher precedence than one with it.
        pre_key = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.pre)
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, pre_key)

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __eq__(self, other) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self):
        return hash(self._key())


def parse(s: str) -> Version | None:
    m = _SEMVER_RE.match(s.strip())
    if not m:
        return None
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    return Version(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre, m.group(5) or "")


_OP_RE = re.compile(r"^(>=|<=|!=|>|<|=|\^|~)?\s*v?([0-9xX*]+(?:\.[0-9xX*]+){0,2}(?:-[0-9A-Za-z.-]+)?)$")


def _partial(s: str) -> tuple[list[int | None], tuple]:
    core, _, pre = s.partition("-")
    nums: list[int | None] = []
    for p in core.split("."):
        nums.append(None if p in ("x", "X", "*") else int(p))
    while len(nums) < 3:
        nums.append(None)
    return nums, tuple(pre.split(".")) if pre else ()


def _comparators(op: str, ver: str) -> list[tuple[str, Version]]:
    nums, pre = _partial(ver)
    known = [n for n in nums if n is not None]
    filled = Version(*(n or 0 for n in nums), pre=pre)
    if op in ("", "=") and len(known) < 3:  # wildcard / partial: 1.2 == 1.2.x
        op = "~" if len(known) == 2 else "^" if len(known) == 1 else "*"
    if op == "*" or not known:
        return []
    if op == "^":
        if nums[0] or len(known) == 1:
            upper = Version(filled.major + 1, 0, 0, ("0",))
        elif nums[1] or len(known) == 2:
            upper = Version(0, filled.minor + 1, 0, ("0",))
        else:
            upper = Version(0, 0, filled.patch + 1, ("0",))
        return [(">=", filled), ("<", upper)]
    if op == "~":
        upper = (Version(filled.major + 1, 0, 0, ("0",)) if len(known) == 1
                 else Version(filled.major, filled.minor + 1, 0, ("0",)))
        return [(">=", filled), ("<", upper)]
    return [(op or "=", filled)]


def _check(op: str, v: Version, c: Version) -> bool:
    return {
        "=": v == c, "!=": v != c, ">": v > c, ">=": v >= c, "<": v < c, "<=": v <= c,
    }[op]


class Constraint:
    def __init__(self, expr: str):
        self.expr = expr
        self.alternatives: list[list[tuple[str, Version]]] = []
        for alt in expr.split("||"):
            comps: list[tuple[str, Version]] = []
            tokens = re.findall(r"(?:>=|<=|!=|>|<|=|\^|~)?\s*v?[0-9xX*][0-9A-Za-z.*-]*", alt.replace(",", " "))
            if not tokens and alt.strip() not in ("", "*"):
                raise ValueError(f"invalid semver constraint: {expr!r}")
            for tok in tokens:
                m = _OP_RE.match(tok.strip())
                if not m:
                    raise ValueError(f"invalid semver constraint term {tok!r} in {expr!r}")
                comps.extend(_comparators(m.group(1) or "", m.group(2)))
            self.alternatives.append(comps)

    def matches(self, v: Version) -> bool:
        # Prerelease filtering is handled separately by the selector (prerelease: include|exclude|only),
        # so ranges compare purely on precedence here.
        return any(all(_check(op, v, c) for op, c in alt) for alt in self.alternatives)

    def __repr__(self):
        return f"Constraint({self.expr!r})"
