"""Copy engine: thin wrapper around `regctl` (github.com/regclient/regclient).

Why regctl instead of hand-written OCI calls: it already handles every corner of the
distribution spec we care about - multi-arch indexes, Helm/OCI artifacts with custom media
types, cross-repo blob mounts, chunked uploads, referrers (cosign signatures / SBOMs),
bearer-token auth flows of Harbor/ACR/ECR/GAR, retries. Swapping it for crane/skopeo/oras
means reimplementing this one module.

Credentials are never put on the command line (visible in `ps`): every invocation gets a
private temporary REGCTL_CONFIG that contains only the hosts that call needs, and an empty
DOCKER_CONFIG so ambient credentials on the box can't leak into a copy.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from .refs import Ref
from .secrets import Credentials

log = logging.getLogger(__name__)

_NOT_FOUND = ("not found", "name_unknown", "manifest_unknown", "404", "status code 404", "unknown manifest")


class EngineError(RuntimeError):
    pass


@dataclass
class HostAuth:
    host: str
    creds: Credentials | None
    tls: str = "enabled"  # enabled | disabled (plain http) | insecure (self-signed)


class RegctlEngine:
    def __init__(self, binary: str = "regctl", timeout: int = 1800, workdir: str | None = None):
        self.binary = shutil.which(binary) or binary
        self.timeout = timeout
        self.workdir = workdir  # put on tmpfs in production

    def _run(self, args: list[str], hosts: list[HostAuth], timeout: int | None = None) -> subprocess.CompletedProcess:
        cfg: dict = {"hosts": {}}
        for h in hosts:
            entry = cfg["hosts"].get(h.host)
            if entry and h.creds and entry.get("user") not in (None, h.creds.username):
                raise EngineError(
                    f"source and destination on the same host {h.host} need different credentials; "
                    "use one robot account with both pull and push rights"
                )
            entry = entry or {"tls": h.tls, "hostname": h.host}
            if h.creds:
                entry["user"], entry["pass"] = h.creds.username, h.creds.password
            cfg["hosts"][h.host] = entry
        with tempfile.TemporaryDirectory(prefix="regctl-", dir=self.workdir) as tmp:
            os.chmod(tmp, 0o700)
            cfg_path = os.path.join(tmp, "config.json")
            fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f)
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": tmp,
                "REGCTL_CONFIG": cfg_path,
                "DOCKER_CONFIG": tmp,
            }
            for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE"):
                if k in os.environ:
                    env[k] = os.environ[k]
            cmd = [self.binary, *args]
            log.debug("exec %s", " ".join(cmd))
            try:
                return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired as e:
                raise EngineError(f"timeout after {e.timeout}s: {' '.join(args)}") from None

    @staticmethod
    def _is_not_found(p: subprocess.CompletedProcess) -> bool:
        err = (p.stderr or "").lower()
        return any(s in err for s in _NOT_FOUND)

    # ------------------------------------------------------------------ operations

    def list_tags(self, repo: str, hosts: list[HostAuth]) -> list[str]:
        p = self._run(["tag", "ls", repo], hosts, timeout=120)
        if p.returncode != 0:
            if self._is_not_found(p):
                return []
            raise EngineError(f"tag ls {repo}: {p.stderr.strip()}")
        return [t for t in p.stdout.split() if t]

    def digest(self, ref: Ref | str, hosts: list[HostAuth], platform: str | None = None) -> str | None:
        args = ["image", "digest", str(ref)]
        if platform:
            args[2:2] = ["--platform", platform]
        p = self._run(args, hosts, timeout=120)
        if p.returncode != 0:
            if self._is_not_found(p):
                return None
            raise EngineError(f"digest {ref}: {p.stderr.strip()}")
        return p.stdout.strip() or None

    def copy(self, src: Ref, dst: Ref, hosts: list[HostAuth], platform: str | None = None,
             referrers: bool = False) -> None:
        args = ["image", "copy"]
        if platform:
            args += ["--platform", platform]
        if referrers:
            args += ["--referrers", "--digest-tags"]
        args += [str(src), str(dst)]
        p = self._run(args, hosts)
        if p.returncode != 0:
            raise EngineError(f"copy {src} -> {dst}: {p.stderr.strip()}")

    def delete_tag(self, ref: Ref, hosts: list[HostAuth]) -> None:
        p = self._run(["tag", "rm", str(ref)], hosts, timeout=120)
        if p.returncode != 0 and not self._is_not_found(p):
            raise EngineError(f"tag rm {ref}: {p.stderr.strip()}")
