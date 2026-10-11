#!/usr/bin/env python3
"""PoC helper playing the role of the shared CI pipeline + Harbor:

  * pushes demo artifacts to the "Harbor" registry (multi-arch images and real Helm charts,
    stored with the exact media types `docker buildx` / `helm push` use)
  * then fires a Harbor-format PUSH_ARTIFACT webhook at the distributor, as Harbor would.

Stdlib only. Speaks the OCI distribution API with basic auth (fine for the PoC registries).

  seed.py push image payments v1.4.0
  seed.py push chart payments 1.4.0
  seed.py scenario initial
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.request

REGISTRY = os.environ.get("SEED_REGISTRY", "harbor:5000")
USER = os.environ.get("SEED_USER", "ci")
PASSWORD = os.environ.get("SEED_PASSWORD", "ci-pass")
SCHEME = os.environ.get("SEED_SCHEME", "http")
WEBHOOK = os.environ.get("SEED_WEBHOOK", "")  # e.g. http://distributor:8080/webhooks/harbor
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
IMAGES = "acme.co/images"
CHARTS = "acme.co/charts"

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"


def sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _req(method: str, url: str, data: bytes | None = None, headers: dict | None = None):
    if not url.startswith("http"):
        url = f"{SCHEME}://{REGISTRY}{url}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode())
    return urllib.request.urlopen(req, timeout=30)


def push_blob(repo: str, blob: bytes) -> dict:
    d = sha(blob)
    try:
        _req("HEAD", f"/v2/{repo}/blobs/{d}")
        return {"digest": d, "size": len(blob)}
    except urllib.error.HTTPError:
        pass
    r = _req("POST", f"/v2/{repo}/blobs/uploads/", b"")
    loc = r.headers["Location"]
    sep = "&" if "?" in loc else "?"
    _req("PUT", f"{loc}{sep}digest={d}", blob, {"Content-Type": "application/octet-stream"})
    return {"digest": d, "size": len(blob)}


def push_manifest(repo: str, ref: str, manifest: dict) -> str:
    body = json.dumps(manifest, separators=(",", ":")).encode()
    _req("PUT", f"/v2/{repo}/manifests/{ref}", body, {"Content-Type": manifest["mediaType"]})
    return sha(body)


def _tgz(files: dict[str, bytes]) -> tuple[bytes, bytes]:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, content in files.items():
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime, ti.mode = len(content), 0, 0o644
            tf.addfile(ti, io.BytesIO(content))
    return raw.getvalue(), gzip.compress(raw.getvalue(), mtime=0)


def push_image(project: str, tag: str, platforms=("amd64", "arm64")) -> str:
    repo = f"{IMAGES}/{project}"
    descs = []
    for arch in platforms:
        tar, layer = _tgz({f"etc/{project}-release": f"{project} {tag} linux/{arch}\n".encode()})
        lyr = push_blob(repo, layer)
        config = json.dumps({
            "architecture": arch, "os": "linux",
            "config": {"Labels": {"org.opencontainers.image.version": tag}},
            "rootfs": {"type": "layers", "diff_ids": [sha(tar)]},
        }).encode()
        cfg = push_blob(repo, config)
        m = {"schemaVersion": 2, "mediaType": OCI_MANIFEST,
             "config": {"mediaType": "application/vnd.oci.image.config.v1+json", **cfg},
             "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", **lyr}]}
        body = json.dumps(m, separators=(",", ":")).encode()
        d = push_manifest(repo, sha(body), m)
        descs.append({"mediaType": OCI_MANIFEST, "digest": d, "size": len(body),
                      "platform": {"architecture": arch, "os": "linux"}})
    index = {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": descs}
    return push_manifest(repo, tag, index)


def push_chart(project: str, version: str) -> str:
    """A real Helm chart, stored exactly like `helm push` does (pullable with `helm pull oci://`)."""
    repo = f"{CHARTS}/{project}"
    semver = version.replace("_", "+")
    chart_yaml = f"apiVersion: v2\nname: {project}\nversion: {semver}\nappVersion: \"{semver}\"\ntype: application\n"
    _, tgz = _tgz({
        f"{project}/Chart.yaml": chart_yaml.encode(),
        f"{project}/values.yaml": f"image:\n  repository: example/{project}\n  tag: \"{semver}\"\n".encode(),
        f"{project}/templates/configmap.yaml":
            b'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}\ndata:\n  tag: {{ .Values.image.tag | quote }}\n',
    })
    lyr = push_blob(repo, tgz)
    cfg = push_blob(repo, json.dumps({"name": project, "version": semver, "apiVersion": "v2",
                                      "appVersion": semver, "type": "application"}).encode())
    m = {"schemaVersion": 2, "mediaType": OCI_MANIFEST,
         "config": {"mediaType": "application/vnd.cncf.helm.config.v1+json", **cfg},
         "layers": [{"mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip", **lyr}],
         "annotations": {"org.opencontainers.image.title": project, "org.opencontainers.image.version": semver}}
    return push_manifest(repo, version, m)


def harbor_webhook(repo_full_name: str, tag: str, digest: str):
    """Harbor 'Default' webhook payload format for PUSH_ARTIFACT."""
    if not WEBHOOK:
        return
    namespace, _, name = repo_full_name.partition("/")
    payload = {
        "type": "PUSH_ARTIFACT", "occur_at": int(time.time()), "operator": USER,
        "event_data": {
            "resources": [{"digest": digest, "tag": tag, "resource_url": f"{REGISTRY}/{repo_full_name}:{tag}"}],
            "repository": {"date_created": int(time.time()), "name": name, "namespace": namespace,
                           "repo_full_name": repo_full_name, "repo_type": "private"},
        },
    }
    req = urllib.request.Request(WEBHOOK, json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    if WEBHOOK_SECRET:
        req.add_header("Authorization", WEBHOOK_SECRET)
    with urllib.request.urlopen(req, timeout=10) as r:
        queued = json.loads(r.read() or b"{}").get("queued", [])
        print(f"  harbor webhook -> HTTP {r.status}, queued: " + (", ".join(f"{j['project']}/{j['kind']}" for j in queued) or "(already pending)"))


def push(kind: str, project: str, tag: str):
    if kind == "image":
        d = push_image(project, tag)
        repo = f"{IMAGES}/{project}"
    else:
        d = push_chart(project, tag)
        repo = f"{CHARTS}/{project}"
    print(f"pushed {kind:<5} {REGISTRY}/{repo}:{tag} {d[:19]}")
    harbor_webhook(repo, tag, d)


SCENARIOS = {
    # What the CI pipeline has published so far.
    "initial": [
        ("image", "payments", t) for t in ("v1.2.0", "v1.3.0", "v1.4.0-rc.1", "v1.4.0", "dev-7f3a9c1")
    ] + [
        ("chart", "payments", t) for t in ("1.2.0", "1.3.0", "1.4.0-rc.1", "1.4.0")
    ] + [
        ("image", "web-ui", t) for t in ("2.0.0", "2.1.0-beta.2", "latest")  # image only, no chart
    ] + [
        ("chart", "observability", t) for t in ("0.9.0", "1.0.0")  # chart only, no image
    ] + [
        ("image", "billing", "1.0.0"),  # project whose config is rejected by policy
    ],
    # A new release: v1.5.0 becomes the newest -> v1.3.0 falls out of keepLatest and gets pruned.
    "release": [("image", "payments", "v1.5.0"), ("chart", "payments", "1.5.0")],
    # A field-test build for the customer.
    "fieldtest": [("image", "payments", "v1.5.1-rc.1")],
}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push")
    p.add_argument("kind", choices=["image", "chart"])
    p.add_argument("project")
    p.add_argument("tag")
    s = sub.add_parser("scenario")
    s.add_argument("name", choices=sorted(SCENARIOS))
    a = ap.parse_args()
    for _ in range(60):  # wait for the registry
        try:
            _req("GET", "/v2/")
            break
        except urllib.error.HTTPError:
            break
        except OSError:
            time.sleep(1)
    items = [(a.kind, a.project, a.tag)] if a.cmd == "push" else SCENARIOS[a.name]
    for kind, project, tag in items:
        push(kind, project, tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
