"""OCI reference model split into the four components that can be renamed independently:

    <registry>/<prefix>/<name>:<tag>
    e.g. harbor.acme.internal / acme.co/images / payments : 1.4.0
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

# Grammar from the OCI distribution spec / docker reference grammar.
_PATH_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
PATH_COMPONENT_RE = re.compile(rf"^{_PATH_COMPONENT}$")
REPOSITORY_RE = re.compile(rf"^{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
REGISTRY_RE = re.compile(
    r"^(?:localhost|[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*)(?::[0-9]+)?$"
)


class RefError(ValueError):
    pass


@dataclass(frozen=True)
class Ref:
    registry: str
    prefix: str  # may be "" (repository directly under the registry root)
    name: str
    tag: str

    @property
    def repository(self) -> str:
        return f"{self.prefix}/{self.name}" if self.prefix else self.name

    @property
    def repo_ref(self) -> str:
        return f"{self.registry}/{self.repository}"

    def __str__(self) -> str:
        return f"{self.registry}/{self.repository}:{self.tag}"

    def with_(self, **kw) -> "Ref":
        return replace(self, **kw)

    def validate(self) -> "Ref":
        if not REGISTRY_RE.match(self.registry):
            raise RefError(f"invalid registry host {self.registry!r} in {self}")
        if not REPOSITORY_RE.match(self.repository):
            raise RefError(
                f"invalid repository {self.repository!r} in {self} "
                "(lowercase alphanumerics separated by '.', '_', '__', '-' and '/')"
            )
        if not TAG_RE.match(self.tag):
            raise RefError(f"invalid tag {self.tag!r} in {self} (OCI tags cannot contain '+'; use '_')")
        return self


def parse(ref: str) -> Ref:
    """Parse 'registry/a/b/name:tag'. The registry part is mandatory (fully qualified refs only)."""
    if "@" in ref:
        raise RefError(f"digest references are not supported as rename targets: {ref}")
    head, _, rest = ref.partition("/")
    if not rest or ("." not in head and ":" not in head and head != "localhost"):
        raise RefError(f"reference must be fully qualified with a registry host: {ref!r}")
    repo, sep, tag = rest.rpartition(":")
    if not sep or "/" in tag:
        raise RefError(f"reference must include a tag: {ref!r}")
    prefix, _, name = repo.rpartition("/")
    return Ref(head, prefix, name, tag).validate()


def path_prefixes(registry: str, repository: str) -> list[str]:
    """Credential lookup keys, most specific first:
    ['reg/a/b/c', 'reg/a/b', 'reg/a', 'reg']"""
    parts = [p for p in repository.split("/") if p]
    keys = [registry + "/" + "/".join(parts[:i]) for i in range(len(parts), 0, -1)]
    return keys + [registry]
