"""Where developer-owned `.distribution.yaml` files come from.

Harbor (and therefore this service) lives behind the VPN, so GitHub cannot push webhooks to
it. The service *pulls* desired state instead (classic GitOps pull model):

* github - scans every repository of a GitHub organization for `.distribution.yaml` on the
           default branch (GitHub App installation token or fine-grained read-only PAT).
* git    - clones/pulls an explicit list of repositories.
* dir    - reads a local directory of checkouts (`<repo>/.distribution.yaml`) or files
           (`<repo>.yaml`). Used by the PoC and by CI (`distributor validate` in a PR).

A broken file in one repository never blocks the others: it is reported and skipped.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import yaml

from .config import ConfigError, PlatformConfig, ProjectConfig, parse_project

log = logging.getLogger(__name__)
DEFAULT_FILE = ".distribution.yaml"


@dataclass
class LoadResult:
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    errors: dict[str, list[str]] = field(default_factory=dict)

    def add(self, repo: str, doc, source: str, platform: PlatformConfig):
        try:
            pc = parse_project(doc, repo, source, platform)
        except ConfigError as e:
            self.errors[repo] = e.errors
            return
        except yaml.YAMLError as e:
            self.errors[repo] = [f"YAML: {e}"]
            return
        if pc.project in self.projects:
            self.errors[repo] = [f"project {pc.project!r} already defined by {self.projects[pc.project].source}"]
            return
        self.projects[pc.project] = pc


def _load_yaml(text: str):
    return yaml.safe_load(text) or {}


def load_dir(path: str, platform: PlatformConfig, filename: str = DEFAULT_FILE) -> LoadResult:
    res = LoadResult()
    for entry in sorted(os.listdir(path)):
        full = os.path.join(path, entry)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, filename)):
            repo, file = entry, os.path.join(full, filename)
        elif os.path.isfile(full) and entry.endswith((".yaml", ".yml")):
            repo, file = entry.rsplit(".", 1)[0], full
        else:
            continue
        try:
            with open(file) as f:
                doc = _load_yaml(f.read())
        except yaml.YAMLError as e:
            res.errors[repo] = [f"YAML: {e}"]
            continue
        res.add(repo, doc, file, platform)
    return res


def load_git(cfg: dict, platform: PlatformConfig) -> LoadResult:
    cache = cfg.get("cacheDir", "/tmp/distributor-git")
    os.makedirs(cache, exist_ok=True)
    filename = cfg.get("file", DEFAULT_FILE)
    res = LoadResult()
    for url in cfg.get("repos", []):
        repo = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        dest = os.path.join(cache, repo)
        try:
            if os.path.isdir(os.path.join(dest, ".git")):
                subprocess.run(["git", "-C", dest, "pull", "--ff-only", "-q"], check=True, capture_output=True, timeout=120)
            else:
                subprocess.run(["git", "clone", "-q", "--depth", "1", url, dest], check=True, capture_output=True, timeout=300)
        except subprocess.SubprocessError as e:
            res.errors[repo] = [f"git: {e}"]
            continue
        file = os.path.join(dest, filename)
        if os.path.isfile(file):
            with open(file) as f:
                res.add(repo, _load_yaml(f.read()), f"{url}:{filename}", platform)
    return res


def load_github(cfg: dict, platform: PlatformConfig) -> LoadResult:
    api = cfg.get("apiUrl", "https://api.github.com").rstrip("/")
    org = cfg["org"]
    filename = cfg.get("file", DEFAULT_FILE)
    token = os.environ.get(cfg.get("tokenEnv", "GITHUB_TOKEN"), "")
    topic = cfg.get("topic")  # optional: only repos tagged with this topic

    def get(url: str):
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "X-GitHub-Api-Version": "2022-11-28"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()), r.headers.get("Link", "")

    res = LoadResult()
    url = f"{api}/orgs/{org}/repos?per_page=100&type=all"
    while url:
        repos, link = get(url)
        for r in repos:
            if r.get("archived") or (topic and topic not in r.get("topics", [])):
                continue
            path = urllib.parse.quote(filename)
            try:
                content, _ = get(f"{api}/repos/{org}/{r['name']}/contents/{path}?ref={r['default_branch']}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    res.errors[r["name"]] = [f"github: HTTP {e.code}"]
                continue
            text = base64.b64decode(content.get("content", "")).decode()
            try:
                res.add(r["name"], _load_yaml(text), f"github.com/{org}/{r['name']}/{filename}", platform)
            except yaml.YAMLError as e:
                res.errors[r["name"]] = [f"YAML: {e}"]
        url = next((p.split(";")[0].strip(" <>") for p in link.split(",") if 'rel="next"' in p), None)
    return res


def load_projects(platform: PlatformConfig) -> LoadResult:
    src = platform.config_source
    kind = src.get("type", "dir")
    if kind == "dir":
        res = load_dir(src["path"], platform, src.get("file", DEFAULT_FILE))
    elif kind == "git":
        res = load_git(src, platform)
    elif kind == "github":
        res = load_github(src, platform)
    else:
        raise ValueError(f"unknown configSource.type {kind!r}")
    for repo, errs in res.errors.items():
        log.warning("config of %s rejected: %s", repo, "; ".join(errs))
    return res
