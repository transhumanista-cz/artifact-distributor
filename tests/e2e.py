#!/usr/bin/env python3
"""End-to-end scenario against a running stack (docker compose, or local binaries).

Walks through every requirement and asserts the outcome by reading the destination
registries directly:

  1. CI pushes artifacts to Harbor -> Harbor webhooks -> automatic, pattern-based distribution
  2. renaming of single components (host, prefix, name, tag) and full references
  3. credentials resolved per registry host + namespace prefix (customer X has 2 accounts)
  4. cost control: only selected tags, single-platform copies, keepLatest + prune
  5. on-demand distribution of a manual rule through the API
  6. GitOps: changing a .distribution.yaml in a repo triggers distribution on its own
  7. policy: forbidden destinations are denied, broken configs are rejected & reported
  8. idempotency: a full reconcile afterwards copies nothing
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("DISTRIBUTOR_URL", "http://distributor:8080")
TOKEN = os.environ.get("API_TOKEN", "")
PUBLIC = os.environ.get("PUBLIC", "public:5000")
CUSTOMER = os.environ.get("CUSTOMER", "customer:5000")
GITOPS = pathlib.Path(os.environ.get("GITOPS_DIR", "/gitops/repos"))
SEED = [sys.executable, str(pathlib.Path(__file__).resolve().parents[1] / "poc/tools/seed.py")]
CUSTOMER_CREDS = {"vendor/acme": ("acme-prod", "prod-pass"), "sandbox/acme": ("acme-sandbox", "sandbox-pass")}

failures = 0


def check(cond: bool, msg: str):
    global failures
    print(("  ok    " if cond else "  FAIL  ") + msg)
    failures += 0 if cond else 1


def api(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(URL + path, json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def tags(registry: str, repo: str) -> list[str]:
    req = urllib.request.Request(f"http://{registry}/v2/{repo}/tags/list")
    for ns, (u, p) in CUSTOMER_CREDS.items():
        if registry == CUSTOMER and repo.startswith(ns):
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return sorted(json.loads(r.read()).get("tags") or [])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def manifest(registry: str, repo: str, ref: str) -> dict:
    req = urllib.request.Request(f"http://{registry}/v2/{repo}/manifests/{ref}", headers={"Accept": ",".join([
        "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json"])})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def idle(timeout: float = 120):
    """Wait until the distributor's job queue is drained and the worker is done."""
    time.sleep(1)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with urllib.request.urlopen(URL + "/metrics", timeout=5) as r:
            m = r.read().decode()
        if "distributor_queue_depth 0" in m:
            # a job taken off the queue still holds the execution lock; a no-op plan waits for it
            api("GET", "/api/v1/plan?project=__none__")
            return
        time.sleep(1)
    raise TimeoutError("distributor did not become idle")


def seed(*args: str):
    subprocess.run(SEED + list(args), check=True, env=os.environ)


def main() -> int:
    for _ in range(120):
        try:
            urllib.request.urlopen(URL + "/healthz", timeout=2)
            break
        except OSError:
            time.sleep(1)

    print("\n[1] CI publishes to Harbor; Harbor webhooks trigger automatic distribution")
    seed("scenario", "initial")
    idle()
    check(tags(PUBLIC, "acme/images/payments") == ["v1.3.0", "v1.4.0"],
          "public gets only stable payments images matching >=1.3.0 (no v1.2.0, rc, dev builds)")
    check(tags(PUBLIC, "acme/charts/payments") == ["1.3.0", "1.4.0"], "...and the matching Helm charts")
    check(tags(PUBLIC, "acme/images/web-ui") == ["2.0.0"], "image-only project web-ui: stable image, no 'latest'")
    check(tags(PUBLIC, "acme/charts/observability") == ["1.0.0"], "chart-only project observability: ^1 -> 1.0.0")

    print("\n[2] renaming + [3] credentials per host + namespace")
    check(tags(CUSTOMER, "vendor/acme/images/acme-payments") == ["1.4.0-rc.1"],
          "rc -> customer-x: name 'acme-${name}', tag regex strips 'v' (account acme-prod)")
    check(tags(CUSTOMER, "sandbox/acme/payments") == ["1.4.0-fieldtest.1"],
          "rc -> customer-x sandbox: tag from regex groups (account acme-sandbox, same host)")
    check(tags(CUSTOMER, "sandbox/acme/webui") == ["2.1-preview"], "full-reference template: webui:${major}.${minor}-preview")
    ev = api("GET", "/api/v1/events?limit=200")
    via = {e["dst"].split("/", 1)[1].rsplit(":", 1)[0]: e["message"] for e in ev if e["action"] == "copied"}
    check(via.get("vendor/acme/images/acme-payments", "").endswith("/vendor/acme")
          and via.get("sandbox/acme/payments", "").endswith("/sandbox/acme"),
          "audit log shows the namespace-specific secret used for each copy")

    print("\n[4] cost control")
    m = manifest(PUBLIC, "acme/images/payments", "v1.4.0")
    check(m["mediaType"].endswith("index.v1+json") and len(m["manifests"]) == 2, "public copy keeps the multi-arch index")
    m = manifest(PUBLIC, "acme/charts/payments", "1.4.0")
    check(m["config"]["mediaType"] == "application/vnd.cncf.helm.config.v1+json", "Helm chart media types preserved")
    seed("scenario", "release")
    idle()
    check(tags(PUBLIC, "acme/images/payments") == ["v1.4.0", "v1.5.0"],
          "new release v1.5.0 -> keepLatest:2 prunes v1.3.0 from the public registry")
    check(tags(PUBLIC, "acme/charts/payments") == ["1.4.0", "1.5.0"], "...same for charts")
    seed("scenario", "fieldtest")
    idle()
    check("1.5.1-rc.1" in tags(CUSTOMER, "vendor/acme/images/acme-payments"), "new field-test build reaches customer X")

    print("\n[5] on-demand (manual rule) via API")
    out = api("POST", "/api/v1/sync", {"project": "payments", "rule": "pilot", "tag": "dev-7f3a9c1"})
    check(out["summary"].get("copied") == 1, f"sync summary {out['summary']}")
    check(tags(PUBLIC, "acme/pilot/images/payments") == ["dev-7f3a9c1"], "dev build handed to the pilot namespace")

    print("\n[6] GitOps: a developer merges a config change")
    f = GITOPS / "observability/.distribution.yaml"
    original = f.read_text()
    try:
        f.write_text(original.replace('semver: "^1"', 'semver: ">=0.9.0"'))
        deadline = time.time() + 90
        while time.time() < deadline and tags(PUBLIC, "acme/charts/observability") != ["0.9.0", "1.0.0"]:
            time.sleep(2)
        check(tags(PUBLIC, "acme/charts/observability") == ["0.9.0", "1.0.0"],
              "widened semver range distributed automatically after the config change")
    finally:
        f.write_text(original)

    print("\n[7] policy & validation")
    denied = [e for e in api("GET", "/api/v1/events?limit=500&project=billing") if e["action"] == "denied"]
    check(bool(denied) and tags(CUSTOMER, "vendor/acme/images/billing") == [],
          "billing -> customer-x denied by policy, nothing copied")
    rejected = api("GET", "/api/v1/projects")["rejected"]
    check("legacy-api" in rejected, f"legacy-api config rejected: {rejected.get('legacy-api', [''])[0][:70]}...")

    print("\n[8] idempotency")
    idle()
    out = api("POST", "/api/v1/reconcile", {"wait": True})
    check("copied" not in out["summary"] and out["summary"].get("skipped", 0) > 0,
          f"full reconcile copies nothing: {out['summary']}")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
