"""Configuration model.

Two kinds of configuration, owned by different people (GitOps separation of concerns):

* PlatformConfig  - owned by the platform team, lives in a central repo. Defines the source
                    (Harbor), the secret backend, named destination *targets* and the *policy*
                    (which project may push to which registry/namespace).
* ProjectConfig   - owned by developers, lives in each project repo as `.distribution.yaml`.
                    Says which of the project's artifacts go where, under which name.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import semver

KINDS = ("image", "chart")
KIND_DIRS = {"image": "images", "chart": "charts"}
API_VERSION = "distribution.acme.co/v1"


class ConfigError(ValueError):
    def __init__(self, errors: list[str] | str, source: str = ""):
        self.errors = [errors] if isinstance(errors, str) else errors
        self.source = source
        super().__init__(f"{source}: " + "; ".join(self.errors) if source else "; ".join(self.errors))


def _expand_env(value: Any) -> Any:
    """Expand ${ENV:NAME} / ${ENV:NAME:-default} in platform config strings (not in project files)."""
    if isinstance(value, str):
        return re.sub(
            r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}",
            lambda m: os.environ.get(m.group(1), m.group(2) or ""),
            value,
        )
    if isinstance(value, dict):
        return {_expand_env(k): _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def glob_match(pattern: str, value: str) -> bool:
    """Path glob: '*' = one path segment, '**' = any number of segments."""
    rx = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            rx += ".*"
            i += 2
        elif pattern[i] == "*":
            rx += "[^/]*"
            i += 1
        else:
            rx += re.escape(pattern[i])
            i += 1
    return re.fullmatch(rx, value) is not None


# --------------------------------------------------------------------------- platform config


@dataclass
class Target:
    name: str
    registry: str
    prefix: str = ""
    description: str = ""


@dataclass
class AllowRule:
    registry: str
    repositories: list[str]
    projects: list[str]

    def permits(self, project: str, registry: str, repository: str) -> bool:
        return (
            fnmatch.fnmatchcase(registry, self.registry)
            and any(fnmatch.fnmatchcase(project, p) for p in self.projects)
            and any(glob_match(r, repository) for r in self.repositories)
        )


@dataclass
class PlatformConfig:
    source_registry: str
    source_repositories: dict[str, str]  # kind -> template with ${project}
    registries: dict[str, dict]  # host -> connection options (tls: enabled|disabled|insecure)
    secrets: dict
    targets: dict[str, Target]
    allow: list[AllowRule]
    require_project_matches_repo: bool
    config_source: dict
    reconcile_interval: int
    copy: dict
    state_path: str
    server: dict
    raw: dict = field(repr=False, default_factory=dict)

    def source_repository(self, kind: str, project: str) -> str:
        return self.source_repositories[kind].replace("${project}", project)

    def kind_and_project_for_repository(self, repository: str) -> tuple[str, str] | None:
        """Reverse mapping used for webhooks: 'acme.co/images/payments' -> ('image', 'payments')."""
        for kind, tmpl in self.source_repositories.items():
            before, _, after = tmpl.partition("${project}")
            if repository.startswith(before) and repository.endswith(after):
                project = repository[len(before): len(repository) - len(after) if after else None]
                if project and "/" not in project:
                    return kind, project
        return None

    def policy_permits(self, project: str, registry: str, repository: str) -> bool:
        return any(a.permits(project, registry, repository) for a in self.allow)


def load_platform(path: str) -> PlatformConfig:
    with open(path) as f:
        raw = _expand_env(yaml.safe_load(f) or {})
    errs: list[str] = []
    src = raw.get("source") or {}
    if not src.get("registry"):
        errs.append("source.registry is required")
    repos = src.get("repositories") or {
        "image": "acme.co/images/${project}",
        "chart": "acme.co/charts/${project}",
    }
    for k, v in repos.items():
        if k not in KINDS:
            errs.append(f"source.repositories: unknown artifact kind {k!r}")
        elif "${project}" not in v:
            errs.append(f"source.repositories.{k} must contain ${{project}}")
    targets = {}
    for name, t in (raw.get("targets") or {}).items():
        if not isinstance(t, dict) or not t.get("registry"):
            errs.append(f"targets.{name}.registry is required")
            continue
        targets[name] = Target(name, t["registry"], t.get("prefix", ""), t.get("description", ""))
    pol = raw.get("policy") or {}
    allow = []
    for i, a in enumerate(pol.get("allow") or []):
        if not a.get("registry"):
            errs.append(f"policy.allow[{i}].registry is required")
            continue
        allow.append(AllowRule(a["registry"], a.get("repositories") or ["**"], a.get("projects") or ["*"]))
    if not allow:
        errs.append("policy.allow must list at least one destination (deny-by-default)")
    secrets = raw.get("secrets") or {"backend": "env"}
    if secrets.get("backend") not in ("vault", "file", "env"):
        errs.append("secrets.backend must be one of vault|file|env")
    if errs:
        raise ConfigError(errs, path)
    return PlatformConfig(
        source_registry=src["registry"],
        source_repositories=repos,
        registries=raw.get("registries") or {},
        secrets=secrets,
        targets=targets,
        allow=allow,
        require_project_matches_repo=pol.get("requireProjectMatchesRepo", True),
        config_source=raw.get("configSource") or {"type": "dir", "path": "./gitops"},
        reconcile_interval=int((raw.get("reconcile") or {}).get("intervalSeconds", 600)),
        copy=raw.get("copy") or {},
        state_path=(raw.get("state") or {}).get("path", "./state.db"),
        server=raw.get("server") or {},
        raw=raw,
    )


# --------------------------------------------------------------------------- project config


@dataclass
class Component:
    """One renameable component: either a ${var} template or a regex substitution of the source value."""

    template: str | None = None
    regex: str | None = None
    replace: str | None = None

    @staticmethod
    def parse(v: Any, where: str, errs: list[str]) -> "Component | None":
        if v is None:
            return None
        if isinstance(v, str):
            return Component(template=v)
        if isinstance(v, dict) and "regex" in v:
            try:
                re.compile(v["regex"])
            except re.error as e:
                errs.append(f"{where}.regex: {e}")
            return Component(regex=v["regex"], replace=str(v.get("replace", "")))
        errs.append(f"{where}: expected a template string or {{regex, replace}}")
        return None


@dataclass
class Destination:
    target: str | None = None
    registry: Component | None = None
    prefix: Component | None = None
    name: Component | None = None
    tag: Component | None = None
    ref: str | None = None


@dataclass
class TagSelector:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    list: list[str] = field(default_factory=list)
    semver: str | None = None
    prerelease: str = "include"  # include | exclude | only
    keep_latest: int | None = None


@dataclass
class Rule:
    name: str
    artifacts: list[str]
    trigger: str  # auto | manual
    tags: TagSelector
    to: list[Destination]
    platform: str | None = None
    referrers: bool = False
    prune: bool = False


@dataclass
class ProjectConfig:
    project: str
    repo: str  # repository the config came from (identity for policy)
    rules: list[Rule]
    source: str = ""  # file path / URL, for messages


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def parse_project(doc: dict, repo: str, source: str, platform: PlatformConfig | None = None) -> ProjectConfig:
    errs: list[str] = []
    if not isinstance(doc, dict):
        raise ConfigError("document must be a mapping", source)
    if doc.get("apiVersion", API_VERSION) != API_VERSION:
        errs.append(f"apiVersion must be {API_VERSION}")
    project = doc.get("project") or repo
    if platform and platform.require_project_matches_repo and project != repo:
        errs.append(f"project {project!r} does not match repository {repo!r} (policy.requireProjectMatchesRepo)")
    defaults = doc.get("defaults") or {}
    rules: list[Rule] = []
    seen: set[str] = set()
    for i, r in enumerate(_as_list(doc.get("rules"))):
        w = f"rules[{i}]"
        r = {**defaults, **(r or {})}
        name = r.get("name") or f"rule-{i}"
        if name in seen:
            errs.append(f"{w}: duplicate rule name {name!r}")
        seen.add(name)
        w = f"rules[{name}]"
        arts = _as_list(r.get("artifacts")) or list(KINDS)
        for a in arts:
            if a not in KINDS:
                errs.append(f"{w}.artifacts: unknown kind {a!r} (expected image|chart)")
        trigger = r.get("trigger", "auto")
        if trigger not in ("auto", "manual"):
            errs.append(f"{w}.trigger must be auto|manual")
        t = r.get("tags") or {}
        sel = TagSelector(
            include=_as_list(t.get("include") or t.get("match")),
            exclude=_as_list(t.get("exclude")),
            list=[str(x) for x in _as_list(t.get("list"))],
            semver=t.get("semver"),
            prerelease=t.get("prerelease", "include"),
            keep_latest=t.get("keepLatest"),
        )
        for rx in sel.include + sel.exclude:
            try:
                re.compile(rx)
            except re.error as e:
                errs.append(f"{w}.tags: bad regex {rx!r}: {e}")
        if sel.semver:
            try:
                semver.Constraint(sel.semver)
            except ValueError as e:
                errs.append(f"{w}.tags.semver: {e}")
        if sel.prerelease not in ("include", "exclude", "only"):
            errs.append(f"{w}.tags.prerelease must be include|exclude|only")
        if trigger == "auto" and not (sel.include or sel.list or sel.semver):
            # Cost guard: never "everything" implicitly - the public registry should hold as little as possible.
            # (Manual rules may omit it: the tag is then given with each on-demand request.)
            errs.append(f"{w}.tags: select tags explicitly with include, list or semver")
        dests: list[Destination] = []
        for j, d in enumerate(_as_list(r.get("to"))):
            dw = f"{w}.to[{j}]"
            if isinstance(d, str):
                d = {"target": d}
            dest = Destination(
                target=d.get("target"),
                registry=Component.parse(d.get("registry"), f"{dw}.registry", errs),
                prefix=Component.parse(d.get("prefix"), f"{dw}.prefix", errs),
                name=Component.parse(d.get("name"), f"{dw}.name", errs),
                tag=Component.parse(d.get("tag"), f"{dw}.tag", errs),
                ref=d.get("ref"),
            )
            if dest.ref and any([dest.registry, dest.prefix, dest.name, dest.tag]):
                errs.append(f"{dw}: 'ref' cannot be combined with registry/prefix/name/tag (only with target)")
            if not dest.ref and not dest.target and not dest.registry:
                errs.append(f"{dw}: needs one of target, registry or ref")
            if dest.target and platform and dest.target not in platform.targets:
                errs.append(f"{dw}: unknown target {dest.target!r} (known: {', '.join(sorted(platform.targets))})")
            dests.append(dest)
        if not dests:
            errs.append(f"{w}.to: at least one destination is required")
        rules.append(
            Rule(
                name=name,
                artifacts=arts,
                trigger=trigger,
                tags=sel,
                to=dests,
                platform=r.get("platform"),
                referrers=bool(r.get("referrers", False)),
                prune=bool(r.get("prune", False)),
            )
        )
    if errs:
        raise ConfigError(errs, source)
    return ProjectConfig(project=project, repo=repo, rules=rules, source=source)


def load_project_file(path: str, repo: str, platform: PlatformConfig | None = None) -> ProjectConfig:
    with open(path) as f:
        return parse_project(yaml.safe_load(f) or {}, repo, path, platform)
