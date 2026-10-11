"""Planner + executor.

plan():    desired state = for every project rule: source tags -> selection -> renamed destinations,
           filtered through the platform policy, with collision detection.
execute(): idempotent convergence - compare digests, copy only what differs, optionally prune
           destination tags this service created earlier and that are no longer desired.
"""
from __future__ import annotations

import dataclasses
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import PlatformConfig, ProjectConfig, Rule
from .engine import EngineError, HostAuth, RegctlEngine
from .refs import Ref, parse
from .rename import RenameError, destination_ref
from .secrets import CredentialResolver
from .selector import select
from .state import State

log = logging.getLogger(__name__)


@dataclass
class Task:
    project: str
    rule: str
    kind: str
    src: Ref
    dst: Ref
    platform: str | None = None
    referrers: bool = False


@dataclass
class PruneTask:
    project: str
    rule: str
    dst: Ref


@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)
    prune: list[PruneTask] = field(default_factory=list)
    denied: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "copy": [{"project": t.project, "rule": t.rule, "kind": t.kind, "src": str(t.src), "dst": str(t.dst)}
                     for t in self.tasks],
            "prune": [{"project": p.project, "rule": p.rule, "dst": str(p.dst)} for p in self.prune],
            "denied": self.denied,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class Result:
    action: str  # copied | skipped | would-copy | pruned | would-prune | failed | denied
    src: str
    dst: str
    project: str
    rule: str
    digest: str = ""
    message: str = ""


class Distributor:
    def __init__(self, platform: PlatformConfig, resolver: CredentialResolver, engine: RegctlEngine, state: State):
        self.platform = platform
        self.resolver = resolver
        self.engine = engine
        self.state = state
        self.concurrency = int(platform.copy.get("concurrency", 4))

    # ------------------------------------------------------------------ helpers

    def _host(self, registry: str, repository: str) -> HostAuth:
        opts = self.platform.registries.get(registry, {})
        return HostAuth(registry, self.resolver.resolve(registry, repository), opts.get("tls", "enabled"))

    def _source_hosts(self, repository: str) -> list[HostAuth]:
        return [self._host(self.platform.source_registry, repository)]

    def source_tags(self, kind: str, project: str) -> list[str]:
        repo = self.platform.source_repository(kind, project)
        return self.engine.list_tags(f"{self.platform.source_registry}/{repo}", self._source_hosts(repo))

    # ------------------------------------------------------------------ planning

    def plan(
        self,
        projects: dict[str, ProjectConfig],
        *,
        project: str | None = None,
        kind: str | None = None,
        rule: str | None = None,
        tag: str | None = None,
        include_manual: bool = False,
    ) -> Plan:
        plan = Plan()
        tag_cache: dict[tuple[str, str], list[str] | None] = {}
        complete: set[tuple[str, str, str]] = set()  # (project, rule, kind) evaluated fully -> safe to prune

        for pc in projects.values():
            if project and pc.project != project:
                continue
            for r in pc.rules:
                if rule and r.name != rule:
                    continue
                if r.trigger == "manual" and not (include_manual or rule == r.name):
                    continue
                for k in r.artifacts:
                    if kind and k != kind:
                        continue
                    key = (k, pc.project)
                    if key not in tag_cache:
                        try:
                            tag_cache[key] = self.source_tags(k, pc.project)
                        except EngineError as e:
                            tag_cache[key] = None
                            plan.errors.append(f"{pc.project}: cannot list source {k} tags: {e}")
                    tags = tag_cache[key]
                    if tags is None:
                        continue
                    self._plan_rule(plan, pc, r, k, tags, tag)
                    if r.prune and tag is None:
                        complete.add((pc.project, r.name, k))

        self._drop_collisions(plan)
        desired = {str(t.dst) for t in plan.tasks}
        for proj, rname in sorted({(p, r) for p, r, _ in complete}):
            for row in self.state.managed(proj, rname):
                mapped = self.platform.kind_and_project_for_repository(parse(row["src"]).repository)
                if mapped and (proj, rname, mapped[0]) in complete and row["dst"] not in desired:
                    plan.prune.append(PruneTask(proj, rname, parse(row["dst"])))
        return plan

    def _plan_rule(self, plan: Plan, pc: ProjectConfig, r: Rule, kind: str, tags: list[str], tag: str | None):
        sel = r.tags
        if tag is not None:
            if sel.list and tag not in sel.list:
                plan.warnings.append(f"{pc.project}/{r.name}: tag {tag} is not in the rule's tag list")
                return
            sel = dataclasses.replace(sel, list=[tag])
        if not (sel.include or sel.list or sel.semver):
            plan.warnings.append(f"{pc.project}/{r.name}: manual rule needs a tag (on-demand request)")
            return
        selected, warns = select(tags, sel)
        plan.warnings += [f"{pc.project}/{r.name}/{kind}: {w}" for w in warns]
        repo = self.platform.source_repository(kind, pc.project)
        prefix, _, name = repo.rpartition("/")
        for s in selected:
            src = Ref(self.platform.source_registry, prefix, name, s.tag)
            for d in r.to:
                try:
                    dst = destination_ref(src, d, kind=kind, project=pc.project, platform=self.platform,
                                          extra_vars=s.vars)
                except RenameError as e:
                    plan.errors.append(f"{pc.project}/{r.name}: {e}")
                    continue
                if not self.platform.policy_permits(pc.project, dst.registry, dst.repository):
                    plan.denied.append({"project": pc.project, "rule": r.name, "src": str(src), "dst": str(dst),
                                        "reason": "destination not allowed by platform policy"})
                    continue
                plan.tasks.append(Task(pc.project, r.name, kind, src, dst, r.platform, r.referrers))

    @staticmethod
    def _drop_collisions(plan: Plan):
        by_dst: dict[str, set[str]] = {}
        for t in plan.tasks:
            by_dst.setdefault(str(t.dst), set()).add(str(t.src))
        bad = {d for d, s in by_dst.items() if len(s) > 1}
        for d in sorted(bad):
            plan.errors.append(f"collision: {', '.join(sorted(by_dst[d]))} would all be written to {d}")
        seen: set[str] = set()
        uniq = []
        for t in plan.tasks:  # same src->dst from two rules is fine, just do it once
            key = f"{t.src}>{t.dst}"
            if str(t.dst) not in bad and key not in seen:
                seen.add(key)
                uniq.append(t)
        plan.tasks = uniq

    # ------------------------------------------------------------------ execution

    def _run_task(self, t: Task, dry_run: bool) -> Result:
        src_hosts = self._source_hosts(t.src.repository)
        dst_host = self._host(t.dst.registry, t.dst.repository)
        hosts = src_hosts + [dst_host]
        base = dict(src=str(t.src), dst=str(t.dst), project=t.project, rule=t.rule)
        try:
            src_digest = self.engine.digest(t.src, src_hosts, t.platform)
            if not src_digest:
                return Result("failed", message="source artifact disappeared", **base)
            dst_digest = self.engine.digest(t.dst, [dst_host])
            if dst_digest == src_digest:
                return Result("skipped", digest=src_digest, message="already up to date", **base)
            if dry_run:
                return Result("would-copy", digest=src_digest,
                              message="new" if not dst_digest else f"differs ({dst_digest[:19]})", **base)
            if dst_host.creds is None:
                log.warning("no credentials for %s/%s, trying anonymously", t.dst.registry, t.dst.repository)
            self.engine.copy(t.src, t.dst, hosts, platform=t.platform, referrers=t.referrers)
            after = self.engine.digest(t.dst, [dst_host])
            if after != src_digest:
                return Result("failed", digest=src_digest, message=f"verification failed: dst digest {after}", **base)
            self.state.record_copy(str(t.dst), str(t.src), src_digest, t.project, t.rule)
            via = dst_host.creds.matched_key if dst_host.creds else "anonymous"
            return Result("copied", digest=src_digest, message=f"credentials: {via}", **base)
        except EngineError as e:
            return Result("failed", message=str(e), **base)

    def _run_prune(self, p: PruneTask, dry_run: bool) -> Result:
        base = dict(src="", dst=str(p.dst), project=p.project, rule=p.rule)
        if dry_run:
            return Result("would-prune", **base)
        try:
            self.engine.delete_tag(p.dst, [self._host(p.dst.registry, p.dst.repository)])
            self.state.forget(str(p.dst))
            return Result("pruned", **base)
        except EngineError as e:
            return Result("failed", message=str(e), **base)

    def execute(self, plan: Plan, dry_run: bool = False) -> list[Result]:
        results: list[Result] = []
        for d in plan.denied:
            results.append(Result("denied", d["src"], d["dst"], d["project"], d["rule"], message=d["reason"]))
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            results += list(pool.map(lambda t: self._run_task(t, dry_run), plan.tasks))
        results += [self._run_prune(p, dry_run) for p in plan.prune]
        if not dry_run:
            for r in results:
                if r.action != "skipped":
                    self.state.event(r.action, r.project, r.rule, r.src, r.dst, r.digest, r.message)
                lvl = logging.WARNING if r.action in ("failed", "denied") else logging.INFO
                if r.action != "skipped":
                    log.log(lvl, "%-7s %s -> %s %s", r.action, r.src or "-", r.dst, r.message)
        return results


def summarize(results: list[Result]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r.action] = out.get(r.action, 0) + 1
    return out

