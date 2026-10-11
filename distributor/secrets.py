"""Credential resolution per registry hostname + namespace prefix.

Credentials are looked up with the most specific key first, so one registry can have
different credentials per namespace (ACR scope-map tokens, Harbor robot accounts per project,
customer registries that give us one account per tenant, ...):

    cr.customer.com/vendor/acme/charts/payments   -> secret at registries/cr.customer.com/vendor/acme/charts/payments
    cr.customer.com/vendor/acme/charts            -> ...
    cr.customer.com/vendor/acme                   -> (found here)
    cr.customer.com/vendor
    cr.customer.com                               -> host-wide fallback

Nothing about credentials appears in developer-owned config: a project only names the
destination; the platform team stores the secret at the matching path.

Backends: HashiCorp Vault / OpenBao KV v2 (token, AppRole or Kubernetes auth), a YAML file
(local dev; can be SOPS-decrypted beforehand), or an env var holding the same YAML.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import yaml

from .config import _expand_env
from .refs import path_prefixes

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    matched_key: str  # which lookup key matched (safe to log)

    def __repr__(self) -> str:  # never leak secrets into logs
        return f"Credentials(username={self.username!r}, matched_key={self.matched_key!r})"


class Backend:
    def get(self, key: str) -> dict | None:
        raise NotImplementedError


class MappingBackend(Backend):
    def __init__(self, data: dict):
        self.data = {k.strip("/"): v for k, v in (data or {}).items()}

    def get(self, key: str) -> dict | None:
        return self.data.get(key)


class FileBackend(MappingBackend):
    def __init__(self, path: str):
        with open(path) as f:
            super().__init__(_expand_env(yaml.safe_load(f) or {}))


class EnvBackend(MappingBackend):
    def __init__(self, var: str = "DISTRIBUTOR_CREDENTIALS"):
        super().__init__(yaml.safe_load(os.environ.get(var, "")) or {})


class VaultBackend(Backend):
    """KV v2 reader for HashiCorp Vault / OpenBao."""

    def __init__(self, address: str, mount: str = "secret", path_prefix: str = "registries",
                 auth: dict | None = None, namespace: str | None = None, timeout: float = 10):
        self.address = address.rstrip("/")
        self.mount = mount.strip("/")
        self.path_prefix = path_prefix.strip("/")
        self.auth = auth or {"method": "token"}
        self.namespace = namespace
        self.timeout = timeout
        self._token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    def _request(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        req = urllib.request.Request(
            f"{self.address}/v1/{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
        )
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Vault-Token", token)
        if self.namespace:
            req.add_header("X-Vault-Namespace", self.namespace)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read() or b"{}")

    def _login(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry:
                return self._token
            a = self.auth
            method = a.get("method", "token")
            if method == "token":
                tok = os.environ.get(a.get("tokenEnv", "VAULT_TOKEN"), "")
                if not tok and a.get("tokenFile"):
                    tok = open(a["tokenFile"]).read().strip()
                self._token, self._token_expiry = tok, float("inf")
            elif method == "approle":
                resp = self._request("POST", f"auth/{a.get('mount', 'approle')}/login", {
                    "role_id": os.environ[a.get("roleIdEnv", "VAULT_ROLE_ID")],
                    "secret_id": os.environ[a.get("secretIdEnv", "VAULT_SECRET_ID")],
                })
                self._set_lease(resp)
            elif method == "kubernetes":
                jwt = open(a.get("jwtFile", "/var/run/secrets/kubernetes.io/serviceaccount/token")).read().strip()
                resp = self._request("POST", f"auth/{a.get('mount', 'kubernetes')}/login",
                                     {"role": a["role"], "jwt": jwt})
                self._set_lease(resp)
            else:
                raise ValueError(f"unsupported vault auth method {method!r}")
            if not self._token:
                raise RuntimeError("vault: no token available")
            return self._token

    def _set_lease(self, resp: dict):
        auth = resp["auth"]
        self._token = auth["client_token"]
        self._token_expiry = time.time() + max(30, int(auth.get("lease_duration", 300)) - 30)

    def get(self, key: str) -> dict | None:
        path = f"{self.mount}/data/{self.path_prefix}/{key}".replace("//", "/")
        try:
            resp = self._request("GET", path, token=self._login())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 403:
                with self._lock:
                    self._token_expiry = 0  # force re-login next time
            raise
        return (resp.get("data") or {}).get("data")


def make_backend(cfg: dict) -> Backend:
    kind = cfg.get("backend", "env")
    if kind == "vault":
        v = cfg.get("vault") or {}
        return VaultBackend(
            address=v.get("address") or os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200"),
            mount=v.get("mount", "secret"),
            path_prefix=v.get("pathPrefix", "registries"),
            auth=v.get("auth"),
            namespace=v.get("namespace"),
        )
    if kind == "file":
        return FileBackend(cfg["file"]["path"])
    return EnvBackend((cfg.get("env") or {}).get("var", "DISTRIBUTOR_CREDENTIALS"))


class CredentialResolver:
    def __init__(self, backend: Backend, ttl: float = 300):
        self.backend = backend
        self.ttl = ttl
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()

    def _get(self, key: str) -> dict | None:
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        val = self.backend.get(key)
        with self._lock:
            self._cache[key] = (now, val)
        return val

    def invalidate(self):
        with self._lock:
            self._cache.clear()

    def resolve(self, registry: str, repository: str) -> Credentials | None:
        for key in path_prefixes(registry, repository):
            data = self._get(key)
            if data:
                user = data.get("username") or data.get("user") or ""
                pw = data.get("password") or data.get("token") or data.get("pass") or ""
                if pw:
                    log.debug("credentials for %s/%s resolved via %s", registry, repository, key)
                    return Credentials(user, pw, key)
        return None
