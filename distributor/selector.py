"""Tag selection: which of a source repository's tags a rule distributes."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import semver
from .config import TagSelector


@dataclass
class Selected:
    tag: str
    vars: dict[str, str] = field(default_factory=dict)  # named regex groups + semver parts


def _semver_vars(v: semver.Version | None) -> dict[str, str]:
    if not v:
        return {}
    return {
        "major": str(v.major),
        "minor": str(v.minor),
        "patch": str(v.patch),
        "prerelease": ".".join(v.pre),
        "version": f"{v.major}.{v.minor}.{v.patch}" + (f"-{'.'.join(v.pre)}" if v.pre else ""),
    }


def select(tags: list[str], sel: TagSelector) -> tuple[list[Selected], list[str]]:
    """Returns (selected tags, warnings). Order: newest semver first, then lexical."""
    warnings: list[str] = []
    candidates = list(tags)
    if sel.list:
        missing = [t for t in sel.list if t not in tags]
        if missing:
            warnings.append(f"listed tags not present in source: {', '.join(missing)}")
        candidates = [t for t in candidates if t in sel.list]

    include = [re.compile(r) for r in sel.include]
    exclude = [re.compile(r) for r in sel.exclude]
    constraint = semver.Constraint(sel.semver) if sel.semver else None

    out: list[Selected] = []
    for tag in candidates:
        vars_: dict[str, str] = {}
        if include:
            m = next((m for rx in include if (m := rx.fullmatch(tag))), None)
            if not m:
                continue
            vars_.update({k: v for k, v in m.groupdict().items() if v is not None})
        if any(rx.search(tag) for rx in exclude):
            continue
        v = semver.parse(tag)
        if constraint and (v is None or not constraint.matches(v)):
            continue
        if v is not None:
            if sel.prerelease == "exclude" and v.is_prerelease:
                continue
            if sel.prerelease == "only" and not v.is_prerelease:
                continue
        elif sel.prerelease == "only":
            continue
        out.append(Selected(tag, {**_semver_vars(v), **vars_}))

    def sort_key(s: Selected):
        v = semver.parse(s.tag)
        return (1, v, "") if v else (0, None, s.tag)

    out.sort(key=lambda s: (sort_key(s)[0], sort_key(s)[1] or semver.Version(0, 0, 0), s.tag), reverse=True)
    if sel.keep_latest:
        non_semver = [s.tag for s in out if semver.parse(s.tag) is None]
        if non_semver:
            warnings.append(f"keepLatest ignores non-semver tags: {', '.join(non_semver)}")
        out = [s for s in out if semver.parse(s.tag) is not None][: sel.keep_latest]
    return out, warnings
