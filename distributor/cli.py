"""Command line entry point.

  distributor validate  [--file .distribution.yaml --repo NAME]   offline check (PR pipelines)
  distributor plan      [--project P] [--rule R] [--tag T] [--manual]   dry run incl. digest diff
  distributor reconcile [--project P]                              converge auto rules once (cron/CI)
  distributor sync      --project P [--rule R] [--tag T]           on-demand, includes manual rules
  distributor creds     REGISTRY/REPOSITORY                        which secret key would be used
  distributor serve                                                webhook + API + GitOps poller
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import ConfigError, load_platform, load_project_file
from .core import Distributor, summarize
from .engine import RegctlEngine
from .gitops import load_projects
from .secrets import CredentialResolver, make_backend
from .state import State


def build(platform_path: str) -> Distributor:
    platform = load_platform(platform_path)
    engine = RegctlEngine(platform.copy.get("binary", "regctl"), int(platform.copy.get("timeoutSeconds", 1800)),
                          platform.copy.get("workdir"))
    resolver = CredentialResolver(make_backend(platform.secrets), float(platform.secrets.get("cacheSeconds", 300)))
    return Distributor(platform, resolver, engine, State(platform.state_path))


def _print_results(out: dict, as_json: bool):
    if as_json:
        print(json.dumps(out, indent=2, default=str))
        return
    for r in out["results"]:
        print(f"{r['action']:<12} {r['src'] or '-':<60} -> {r['dst']}  {r['message']}")
    for e in out["errors"]:
        print(f"ERROR   {e}", file=sys.stderr)
    for w in out["warnings"]:
        print(f"WARNING {w}", file=sys.stderr)
    print(f"summary: {out['summary'] or 'nothing to do'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="distributor")
    ap.add_argument("--platform", default=os.environ.get("DISTRIBUTOR_CONFIG", "platform.yaml"),
                    help="platform config (env DISTRIBUTOR_CONFIG)")
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate configs without touching registries")
    v.add_argument("--file", help="single .distribution.yaml to validate (default: all from configSource)")
    v.add_argument("--repo", help="repository name the file belongs to (default: parent directory name)")

    for name in ("plan", "reconcile", "sync"):
        p = sub.add_parser(name)
        p.add_argument("--project")
        p.add_argument("--json", action="store_true")
        if name != "reconcile":
            p.add_argument("--rule")
            p.add_argument("--tag")
            p.add_argument("--kind", choices=["image", "chart"])
        if name == "plan":
            p.add_argument("--manual", action="store_true", help="include manual rules")

    c = sub.add_parser("creds", help="show which credential key resolves for a repository")
    c.add_argument("repository", help="registry/namespace/name")
    sub.add_parser("serve")

    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        if a.cmd == "validate":
            platform = load_platform(a.platform)
            if a.file:
                repo = a.repo or os.path.basename(os.path.dirname(os.path.abspath(a.file)))
                pc = load_project_file(a.file, repo, platform)
                print(f"OK  {a.file}: project {pc.project}, rules: {', '.join(r.name for r in pc.rules)}")
                return 0
            res = load_projects(platform)
            for name, pc in sorted(res.projects.items()):
                print(f"OK  {name}: {', '.join(r.name for r in pc.rules)}")
            for repo, errs in sorted(res.errors.items()):
                for e in errs:
                    print(f"ERR {repo}: {e}")
            return 1 if res.errors else 0

        dist = build(a.platform)
        if a.cmd == "creds":
            reg, _, repo = a.repository.partition("/")
            cr = dist.resolver.resolve(reg, repo)
            print(f"{a.repository}: " + (f"user={cr.username!r} via key {cr.matched_key!r}" if cr else "no credentials"))
            return 0 if cr else 1

        from .server import App, Job, serve

        app = App(dist.platform, dist)
        if a.cmd == "serve":
            serve(app)
            return 0
        app.reload()  # rejected project configs are logged by the loader
        if a.cmd == "plan":
            job = Job("cli-plan", a.project, a.kind, a.rule, a.tag, a.manual or bool(a.rule))
            out = app.run_job(job, dry_run=True)
        elif a.cmd == "reconcile":
            out = app.run_job(Job("cli-reconcile", a.project))
        else:
            if not a.project:
                ap.error("sync requires --project")
            out = app.run_job(Job("cli-sync", a.project, a.kind, a.rule, a.tag, include_manual=True))
        _print_results(out, a.json)
        failed = out["summary"].get("failed", 0) + len(out["errors"])
        return 1 if failed else 0
    except ConfigError as e:
        for err in e.errors:
            print(f"ERR {e.source}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
