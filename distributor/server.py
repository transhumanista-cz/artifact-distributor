"""Long-running service: Harbor webhooks, on-demand API, GitOps polling and periodic reconcile.

Triggers
  * Harbor webhook (PUSH_ARTIFACT)  -> reconcile the pushed project/kind     (automatic, seconds)
  * config change detected in git   -> reconcile the changed project         (GitOps, ~poll interval)
  * periodic full reconcile         -> heals anything missed (webhook lost, registry down, ...)
  * POST /api/v1/sync               -> on-demand, also runs `trigger: manual` rules
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import PlatformConfig, ProjectConfig
from .core import Distributor, summarize
from .gitops import load_projects

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    reason: str
    project: str | None = None
    kind: str | None = None
    rule: str | None = None
    tag: str | None = None
    include_manual: bool = False

    def __str__(self):
        extra = "".join(f" {k}={v}" for k, v in (("rule", self.rule), ("tag", self.tag)) if v)
        return f"{self.reason}[{self.project or '*'}/{self.kind or '*'}{extra}]"

    @property
    def key(self):
        return (self.project, self.kind, self.rule, self.tag, self.include_manual)


class App:
    def __init__(self, platform: PlatformConfig, dist: Distributor):
        self.platform = platform
        self.dist = dist
        self.projects: dict[str, ProjectConfig] = {}
        self.config_errors: dict[str, list[str]] = {}
        self._fingerprints: dict[str, str] = {}
        self.queue: queue.Queue[Job] = queue.Queue()
        self._pending: set = set()
        self._pending_lock = threading.Lock()
        self.exec_lock = threading.Lock()  # one convergence at a time
        self.metrics = {"reconciles": 0, "last_reconcile_ts": 0.0, "last_reconcile_seconds": 0.0,
                        "webhooks": 0, "webhooks_rejected": 0}
        self.poll_seconds = int(platform.config_source.get("pollSeconds", 60))
        self.stop = threading.Event()

    # ------------------------------------------------------------------ config (GitOps pull)

    def reload(self) -> list[str]:
        """Reload project configs; returns projects whose config changed (new or modified)."""
        res = load_projects(self.platform)
        changed = []
        fps = {}
        for name, pc in res.projects.items():
            fp = repr(pc.rules)
            fps[name] = fp
            if self._fingerprints.get(name) != fp:
                changed.append(name)
        self.projects, self.config_errors, self._fingerprints = res.projects, res.errors, fps
        return changed

    # ------------------------------------------------------------------ jobs

    def enqueue(self, job: Job) -> bool:
        with self._pending_lock:
            if job.key in self._pending:
                return False
            self._pending.add(job.key)
        self.queue.put(job)
        return True

    def run_job(self, job: Job, dry_run: bool = False) -> dict:
        with self.exec_lock:
            t0 = time.time()
            plan = self.dist.plan(self.projects, project=job.project, kind=job.kind, rule=job.rule,
                                  tag=job.tag, include_manual=job.include_manual)
            for e in plan.errors:
                log.error("plan: %s", e)
            results = self.dist.execute(plan, dry_run=dry_run)
            dt = time.time() - t0
            if job.project is None and not dry_run:
                self.metrics["reconciles"] += 1
                self.metrics["last_reconcile_ts"] = time.time()
                self.metrics["last_reconcile_seconds"] = dt
            summary = summarize(results)
            log.info("job %s done in %.1fs: %s", job, dt, summary or "nothing to do")
            return {"job": asdict(job), "summary": summary, "errors": plan.errors, "warnings": plan.warnings,
                    "results": [asdict(r) for r in results]}

    def worker(self):
        while not self.stop.is_set():
            try:
                job = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            with self._pending_lock:
                self._pending.discard(job.key)
            try:
                self.run_job(job)
            except Exception:
                log.exception("job %s failed", job)

    def scheduler(self):
        last_full = 0.0
        while not self.stop.is_set():
            try:
                changed = self.reload()
                if time.time() - last_full >= self.platform.reconcile_interval:
                    last_full = time.time()
                    self.enqueue(Job("periodic"))
                else:
                    for p in changed:
                        log.info("config of %s changed -> reconcile", p)
                        self.enqueue(Job("config-changed", project=p))
            except Exception:
                log.exception("config poll failed")
            self.stop.wait(self.poll_seconds)

    # ------------------------------------------------------------------ webhook parsing

    def jobs_from_harbor(self, payload: dict) -> list[Job]:
        """Harbor 'Default' payload and 'CloudEvents' payload formats."""
        etype = payload.get("type", "")
        data = payload.get("event_data") or payload.get("data") or {}
        if etype not in ("PUSH_ARTIFACT", "harbor.artifact.pushed"):
            return []
        repo = (data.get("repository") or {}).get("repo_full_name", "")
        mapped = self.platform.kind_and_project_for_repository(repo)
        if not mapped:
            log.info("webhook for unmanaged repository %s ignored", repo)
            return []
        kind, project = mapped
        if project not in self.projects:
            return []
        return [Job("webhook", project=project, kind=kind)]


def make_handler(app: App):
    srv = app.platform.server
    webhook_secret = os.environ.get(srv.get("webhookSecretEnv", "WEBHOOK_SECRET"), "")
    api_token = os.environ.get(srv.get("apiTokenEnv", "API_TOKEN"), "")

    class Handler(BaseHTTPRequestHandler):
        server_version = "artifact-distributor"

        def log_message(self, fmt, *args):
            log.debug("http %s", fmt % args)

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, (bytes, str)) else json.dumps(body, indent=2, default=str)
            data = data.encode() if isinstance(data, str) else data
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

        def _authorized(self, secret: str, header: str = "Authorization") -> bool:
            if not secret:
                return True  # auth disabled (PoC only - always set it in production)
            got = self.headers.get(header, "")
            return hmac.compare_digest(got.removeprefix("Bearer ").strip(), secret)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/healthz":
                return self._send(200, {"status": "ok"})
            if u.path == "/metrics":
                return self._send(200, render_metrics(app), "text/plain; version=0.0.4")
            if not self._authorized(api_token):
                return self._send(401, {"error": "unauthorized"})
            if u.path == "/api/v1/projects":
                return self._send(200, {
                    "projects": {n: {"source": p.source, "rules": [r.name for r in p.rules]}
                                 for n, p in app.projects.items()},
                    "rejected": app.config_errors,
                })
            if u.path == "/api/v1/plan":
                job = Job("plan", project=q.get("project"), rule=q.get("rule"), tag=q.get("tag"),
                          include_manual=q.get("manual") == "true")
                return self._send(200, app.run_job(job, dry_run=True))
            if u.path == "/api/v1/events":
                return self._send(200, app.dist.state.events(int(q.get("limit", 100)), q.get("project")))
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            try:
                body = self._body()
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})
            if u.path == "/webhooks/harbor":
                if not self._authorized(webhook_secret):
                    app.metrics["webhooks_rejected"] += 1
                    return self._send(401, {"error": "unauthorized"})
                app.metrics["webhooks"] += 1
                jobs = app.jobs_from_harbor(body)
                queued = [asdict(j) for j in jobs if app.enqueue(j)]
                return self._send(202, {"queued": queued})
            if not self._authorized(api_token):
                return self._send(401, {"error": "unauthorized"})
            if u.path == "/api/v1/reload":
                return self._send(200, {"changed": app.reload(), "rejected": app.config_errors})
            if u.path == "/api/v1/reconcile":
                job = Job("api-reconcile", project=body.get("project"))
                if body.get("wait"):
                    return self._send(200, app.run_job(job))
                app.enqueue(job)
                return self._send(202, {"queued": asdict(job)})
            if u.path == "/api/v1/sync":
                if not body.get("project"):
                    return self._send(400, {"error": "project is required"})
                if body["project"] not in app.projects:
                    return self._send(404, {"error": f"unknown project {body['project']!r}",
                                            "rejected": app.config_errors.get(body["project"])})
                job = Job("on-demand", project=body["project"], rule=body.get("rule"), kind=body.get("kind"),
                          tag=body.get("tag"), include_manual=True)
                return self._send(200, app.run_job(job, dry_run=bool(body.get("dryRun"))))
            return self._send(404, {"error": "not found"})

    return Handler


def render_metrics(app: App) -> str:
    lines = [
        "# TYPE distributor_events_total counter",
        *[f'distributor_events_total{{action="{a}"}} {n}' for a, n in app.dist.state.counters().items()],
        "# TYPE distributor_reconciles_total counter",
        f"distributor_reconciles_total {app.metrics['reconciles']}",
        f"distributor_last_reconcile_timestamp_seconds {app.metrics['last_reconcile_ts']}",
        f"distributor_last_reconcile_duration_seconds {app.metrics['last_reconcile_seconds']}",
        f"distributor_webhooks_total {app.metrics['webhooks']}",
        f"distributor_webhooks_rejected_total {app.metrics['webhooks_rejected']}",
        f"distributor_projects {len(app.projects)}",
        f"distributor_projects_rejected {len(app.config_errors)}",
        f"distributor_queue_depth {app.queue.qsize()}",
    ]
    return "\n".join(lines) + "\n"


def serve(app: App):
    listen = app.platform.server.get("listen", "0.0.0.0:8080")
    host, _, port = listen.rpartition(":")
    app.reload()
    threading.Thread(target=app.worker, daemon=True, name="worker").start()
    threading.Thread(target=app.scheduler, daemon=True, name="scheduler").start()
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port)), make_handler(app))
    log.info("listening on %s (%d projects, %d rejected configs)", listen, len(app.projects), len(app.config_errors))
    try:
        httpd.serve_forever()
    finally:
        app.stop.set()
