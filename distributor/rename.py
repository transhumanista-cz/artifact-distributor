"""Computes the destination reference for a source artifact.

Every component (registry, prefix, name, tag) can be renamed independently; anything not
specified is inherited. Resolution order:

    1. start from the source:            harbor.acme/acme.co/images/payments:1.4.0
    2. apply the target alias (if any):  registry + prefix from platform `targets`
    3. apply per-component overrides:    template "${name}-svc" or {regex, replace} on the source value
    4. or replace everything with `ref`: "cr.customer.com/acme/${name}:${major}.${minor}"

Template variables always refer to SOURCE values (${registry} ${prefix} ${name} ${tag}), plus
${project} ${kind} ${kind_dir} ${target_registry} ${target_prefix}, semver parts
(${major} ${minor} ${patch} ${prerelease} ${version}) and named groups from the rule's tag regex.
"""
from __future__ import annotations

import re
from string import Template

from .config import KIND_DIRS, Component, Destination, PlatformConfig
from .refs import Ref, RefError, parse


class RenameError(ValueError):
    pass


def _render(tmpl: str, vars_: dict[str, str], what: str) -> str:
    try:
        return Template(tmpl).substitute(vars_)
    except KeyError as e:
        raise RenameError(
            f"{what}: unknown variable ${{{e.args[0]}}} in {tmpl!r}; available: {', '.join(sorted(vars_))}"
        ) from None
    except ValueError as e:
        raise RenameError(f"{what}: bad template {tmpl!r}: {e}") from None


def _apply(c: Component | None, current: str, source_value: str, vars_: dict[str, str], what: str) -> str:
    if c is None:
        return current
    if c.template is not None:
        return _render(c.template, vars_, what)
    # regex substitution applied to the source value of this component
    return re.sub(c.regex, c.replace, source_value)


def destination_ref(
    src: Ref,
    dest: Destination,
    *,
    kind: str,
    project: str,
    platform: PlatformConfig,
    extra_vars: dict[str, str] | None = None,
) -> Ref:
    vars_ = {
        "registry": src.registry,
        "prefix": src.prefix,
        "name": src.name,
        "tag": src.tag,
        "project": project,
        "kind": kind,
        "kind_dir": KIND_DIRS[kind],
        "target_registry": "",
        "target_prefix": "",
        **(extra_vars or {}),
    }
    out = src
    if dest.target:
        t = platform.targets[dest.target]
        tprefix = _render(t.prefix, vars_, f"targets.{t.name}.prefix")
        vars_["target_registry"], vars_["target_prefix"] = t.registry, tprefix
        out = out.with_(registry=t.registry, prefix=tprefix)

    if dest.ref:  # full-reference template (may use ${target_registry}/${target_prefix} of a target)
        rendered = _render(dest.ref, vars_, "ref")
        try:
            return parse(rendered)
        except RefError as e:
            raise RenameError(str(e)) from None
    out = out.with_(
        registry=_apply(dest.registry, out.registry, src.registry, vars_, "registry"),
        prefix=_apply(dest.prefix, out.prefix, src.prefix, vars_, "prefix").strip("/"),
        name=_apply(dest.name, out.name, src.name, vars_, "name"),
        tag=_apply(dest.tag, out.tag, src.tag, vars_, "tag"),
    )
    # Names produced by templates may legitimately contain '/', e.g. name: "team/${name}".
    if "/" in out.name:
        extra, _, nm = out.name.rpartition("/")
        out = out.with_(prefix=f"{out.prefix}/{extra}".strip("/"), name=nm)
    try:
        return out.validate()
    except RefError as e:
        raise RenameError(str(e)) from None
