import textwrap

import pytest
import yaml

from distributor import semver
from distributor.config import ConfigError, Destination, Component, TagSelector, load_platform, parse_project
from distributor.core import Distributor
from distributor.refs import Ref, RefError, parse, path_prefixes
from distributor.rename import RenameError, destination_ref
from distributor.secrets import CredentialResolver, MappingBackend
from distributor.selector import select
from distributor.server import App
from distributor.state import State

PLATFORM = """
source:
  registry: harbor.acme.internal
  repositories:
    image: acme.co/images/${project}
    chart: acme.co/charts/${project}
targets:
  public:   {registry: acmepublic.azurecr.io, prefix: "acme/${kind_dir}"}
  customer-x: {registry: cr.customer-x.com, prefix: "vendor/acme"}
policy:
  allow:
    - registry: acmepublic.azurecr.io
      repositories: ["acme/**"]
    - registry: cr.customer-x.com
      repositories: ["vendor/acme/**"]
      projects: ["pay*"]
secrets: {backend: env}
"""


@pytest.fixture
def platform(tmp_path):
    p = tmp_path / "platform.yaml"
    p.write_text(PLATFORM)
    return load_platform(str(p))


SRC = Ref("harbor.acme.internal", "acme.co/images", "payments", "v1.4.0")


# ----------------------------------------------------------------- refs


def test_parse_and_components():
    r = parse("cr.example.com:5000/a/b/name:1.0")
    assert (r.registry, r.prefix, r.name, r.tag) == ("cr.example.com:5000", "a/b", "name", "1.0")
    assert str(r) == "cr.example.com:5000/a/b/name:1.0"
    with pytest.raises(RefError):
        parse("library/nginx:1")  # not fully qualified
    with pytest.raises(RefError):
        parse("reg.io/x/Upper:1")
    with pytest.raises(RefError):
        Ref("reg.io", "", "x", "1.0+build").validate()


def test_path_prefixes():
    assert path_prefixes("reg", "a/b/c") == ["reg/a/b/c", "reg/a/b", "reg/a", "reg"]


# ----------------------------------------------------------------- semver


@pytest.mark.parametrize("expr,ver,ok", [
    (">=1.2.0 <2.0.0", "1.9.9", True),
    (">=1.2.0 <2.0.0", "2.0.0", False),
    ("^1.4", "1.99.0", True),
    ("^1.4", "2.0.0-rc.1", False),
    ("^0.4.1", "0.5.0", False),
    ("~1.4.2", "1.4.9", True),
    ("~1.4.2", "1.5.0", False),
    ("1.x", "1.7.3", True),
    (">=2.0.0 || 1.9.x", "1.9.4", True),
    (">=2.0.0 || 1.9.x", "1.8.0", False),
    (">=1.0.0", "v1.3.0-rc.2", True),
])
def test_semver_constraints(expr, ver, ok):
    assert semver.Constraint(expr).matches(semver.parse(ver)) is ok


def test_semver_order():
    vs = ["1.0.0", "1.0.0-rc.1", "1.0.0-alpha", "1.0.0-rc.10", "0.9.0", "1.0.0-rc.2"]
    assert [str(semver.parse(v).pre) for v in sorted(vs, key=semver.parse)] == \
        ["()", "('alpha',)", "('rc', '1')", "('rc', '2')", "('rc', '10')", "()"]
    assert semver.parse("1.2.3_build.5").build == "build.5"  # helm '+' -> '_'


# ----------------------------------------------------------------- selector


TAGS = ["latest", "v1.2.0", "v1.3.0", "v1.4.0-rc.1", "v1.4.0", "v2.0.0-beta.1", "dev-abc123", "1.4.0-field.3"]


def tags_of(sel):
    return [s.tag for s in select(TAGS, sel)[0]]


def test_selector_semver_and_prerelease():
    assert tags_of(TagSelector(semver=">=1.3.0", prerelease="exclude")) == ["v1.4.0", "v1.3.0"]
    assert tags_of(TagSelector(semver=">=1.3.0", prerelease="only")) == ["v2.0.0-beta.1", "v1.4.0-rc.1", "1.4.0-field.3"]


def test_selector_regex_groups_exclude_keep_latest():
    out, _ = select(TAGS, TagSelector(include=[r"(?P<ver>\d+\.\d+\.\d+)-field\.(?P<n>\d+)"]))
    assert out[0].tag == "1.4.0-field.3" and out[0].vars["ver"] == "1.4.0" and out[0].vars["n"] == "3"
    assert tags_of(TagSelector(include=[r"v.*"], exclude=["-rc", "beta"])) == ["v1.4.0", "v1.3.0", "v1.2.0"]
    assert tags_of(TagSelector(include=[r"v.*"], prerelease="exclude", keep_latest=2)) == ["v1.4.0", "v1.3.0"]


def test_selector_list_warns_on_missing():
    out, warns = select(TAGS, TagSelector(list=["v1.2.0", "v9.9.9"]))
    assert [s.tag for s in out] == ["v1.2.0"] and "v9.9.9" in warns[0]


# ----------------------------------------------------------------- rename


def ren(platform, dest, kind="image", **vars_):
    src = SRC if kind == "image" else SRC.with_(prefix="acme.co/charts")
    return str(destination_ref(src, dest, kind=kind, project="payments", platform=platform, extra_vars=vars_))


def test_rename_target_inherits_name_and_tag(platform):
    assert ren(platform, Destination(target="public")) == "acmepublic.azurecr.io/acme/images/payments:v1.4.0"
    assert ren(platform, Destination(target="public"), kind="chart") == "acmepublic.azurecr.io/acme/charts/payments:v1.4.0"


def test_rename_single_components(platform):
    d = Destination(target="customer-x", name=Component(template="acme-${name}"),
                    tag=Component(regex=r"^v", replace=""))
    assert ren(platform, d) == "cr.customer-x.com/vendor/acme/acme-payments:1.4.0"
    d = Destination(target="customer-x", prefix=Component(template="${target_prefix}/stable"))
    assert ren(platform, d) == "cr.customer-x.com/vendor/acme/stable/payments:v1.4.0"
    d = Destination(registry=Component(template="mirror.local:5000"))  # only the hostname
    assert ren(platform, d) == "mirror.local:5000/acme.co/images/payments:v1.4.0"
    d = Destination(target="public", name=Component(template="team/${project}-svc"))  # '/' in name
    assert ren(platform, d) == "acmepublic.azurecr.io/acme/images/team/payments-svc:v1.4.0"


def test_rename_full_ref_with_semver_vars(platform):
    d = Destination(ref="cr.customer-x.com/vendor/acme/${kind_dir}/${name}:${major}.${minor}")
    assert ren(platform, d, major="1", minor="4") == "cr.customer-x.com/vendor/acme/images/payments:1.4"


def test_rename_errors(platform):
    with pytest.raises(RenameError, match="unknown variable"):
        ren(platform, Destination(target="public", tag=Component(template="${nope}")))
    with pytest.raises(RenameError, match="invalid repository"):
        ren(platform, Destination(target="public", name=Component(template="Bad_Name!")))


# ----------------------------------------------------------------- policy / config


def test_policy(platform):
    assert platform.policy_permits("payments", "cr.customer-x.com", "vendor/acme/payments")
    assert not platform.policy_permits("billing", "cr.customer-x.com", "vendor/acme/billing")  # project glob
    assert not platform.policy_permits("payments", "cr.customer-x.com", "other/payments")
    assert not platform.policy_permits("payments", "evil.example.com", "acme/x")


def test_webhook_repo_mapping(platform):
    assert platform.kind_and_project_for_repository("acme.co/images/payments") == ("image", "payments")
    assert platform.kind_and_project_for_repository("acme.co/charts/payments") == ("chart", "payments")
    assert platform.kind_and_project_for_repository("acme.co/other/payments") is None


def test_project_validation(platform):
    good = yaml.safe_load(textwrap.dedent("""
        rules:
          - name: public
            tags: {semver: ">=1.0.0", prerelease: exclude}
            to: [public]
    """))
    pc = parse_project(good, "payments", "x", platform)
    assert pc.project == "payments" and pc.rules[0].artifacts == ["image", "chart"]

    bad = {"project": "other", "rules": [{"name": "a", "to": [{"target": "nope"}]}, {"name": "a", "tags": {"semver": "garbage"}}]}
    with pytest.raises(ConfigError) as e:
        parse_project(bad, "payments", "x", platform)
    msg = " | ".join(e.value.errors)
    for needle in ("does not match repository", "unknown target", "select tags explicitly",
                   "duplicate rule name", "semver", "at least one destination"):
        assert needle in msg, needle


# ----------------------------------------------------------------- secrets


def test_credentials_most_specific_wins():
    r = CredentialResolver(MappingBackend({
        "cr.customer-x.com": {"username": "host-wide", "password": "a"},
        "cr.customer-x.com/vendor/acme": {"username": "tenant", "password": "b"},
    }))
    assert r.resolve("cr.customer-x.com", "vendor/acme/charts/payments").username == "tenant"
    assert r.resolve("cr.customer-x.com", "other/x").username == "host-wide"
    assert r.resolve("unknown.io", "x") is None
    assert "b" not in repr(r.resolve("cr.customer-x.com", "vendor/acme/x"))


# ----------------------------------------------------------------- planner with a fake registry


class FakeEngine:
    def __init__(self, tags):
        self.tags = tags  # repo -> [tags]
        self.copied, self.deleted = [], []

    def list_tags(self, repo, hosts):
        return self.tags.get(repo, [])

    def digest(self, ref, hosts, platform=None):
        s = str(ref)
        if s.startswith("harbor"):
            return "sha256:" + s.rsplit(":", 1)[1]
        return next(("sha256:" + src.rsplit(":", 1)[1] for src, dst in self.copied if dst == s), None)

    def copy(self, src, dst, hosts, platform=None, referrers=False):
        self.copied.append((str(src), str(dst)))

    def delete_tag(self, ref, hosts):
        self.deleted.append(str(ref))


def make_dist(platform, tags):
    return Distributor(platform, CredentialResolver(MappingBackend({})), FakeEngine(tags), State(":memory:"))


def projects(platform, doc):
    return {"payments": parse_project(yaml.safe_load(doc), "payments", "x", platform)}


def test_plan_execute_idempotent_and_prune(platform):
    tags = {"harbor.acme.internal/acme.co/images/payments": ["1.0.0", "1.1.0", "1.2.0", "1.3.0-rc.1"],
            "harbor.acme.internal/acme.co/charts/payments": ["1.2.0"]}
    d = make_dist(platform, tags)
    cfg = projects(platform, """
        rules:
          - name: public
            tags: {semver: ">=1.0.0", prerelease: exclude, keepLatest: 2}
            prune: true
            to: [public]
          - name: denied
            artifacts: [image]
            tags: {list: ["1.2.0"]}
            to: [{registry: evil.example.com}]
          - name: pilot
            trigger: manual
            artifacts: [image]
            to: [{target: public, prefix: "acme/pilot"}]
    """)
    plan = d.plan(cfg)
    assert sorted(str(t.dst) for t in plan.tasks) == [
        "acmepublic.azurecr.io/acme/charts/payments:1.2.0",
        "acmepublic.azurecr.io/acme/images/payments:1.1.0",
        "acmepublic.azurecr.io/acme/images/payments:1.2.0",
    ]
    assert plan.denied and plan.denied[0]["dst"].startswith("evil.example.com")
    res = d.execute(plan)
    assert sorted(r.action for r in res) == ["copied", "copied", "copied", "denied"]
    assert all(r.action == "skipped" for r in d.execute(d.plan(cfg)) if r.action != "denied")

    # a new release arrives -> 1.1.0 falls out of keepLatest:2 and is pruned
    tags["harbor.acme.internal/acme.co/images/payments"].append("1.3.0")
    plan = d.plan(cfg, project="payments", kind="image")  # what a Harbor webhook for the image triggers
    assert [str(p.dst) for p in plan.prune] == ["acmepublic.azurecr.io/acme/images/payments:1.1.0"]
    plan = d.plan(cfg, project="payments", kind="chart")  # chart side untouched by an image push
    assert not plan.prune
    plan = d.plan(cfg)
    assert [str(p.dst) for p in plan.prune] == ["acmepublic.azurecr.io/acme/images/payments:1.1.0"]
    d.execute(plan)
    assert d.engine.deleted == ["acmepublic.azurecr.io/acme/images/payments:1.1.0"]

    # manual rule only runs on demand, with an explicit tag
    assert not [t for t in d.plan(cfg).tasks if t.rule == "pilot"]
    plan = d.plan(cfg, project="payments", rule="pilot", tag="1.3.0-rc.1")
    assert [str(t.dst) for t in plan.tasks] == ["acmepublic.azurecr.io/acme/pilot/payments:1.3.0-rc.1"]
    assert not plan.prune  # partial evaluation never prunes


def test_collision_detection(platform):
    d = make_dist(platform, {"harbor.acme.internal/acme.co/images/payments": ["1.0.0"],
                             "harbor.acme.internal/acme.co/charts/payments": ["1.0.0"]})
    cfg = projects(platform, """
        rules:
          - name: flat
            tags: {list: ["1.0.0"]}
            to: [{target: customer-x}]   # images and charts would land in the same repository
    """)
    plan = d.plan(cfg)
    assert not plan.tasks and "collision" in plan.errors[0]


def test_webhook_payloads(platform):
    d = make_dist(platform, {})
    app = App(platform, d)
    app.projects = projects(platform, "rules: [{name: a, tags: {list: [x]}, to: [public]}]")
    default = {"type": "PUSH_ARTIFACT", "event_data": {"repository": {"repo_full_name": "acme.co/charts/payments"},
                                                       "resources": [{"tag": "1.0.0"}]}}
    cloud = {"specversion": "1.0", "type": "harbor.artifact.pushed",
             "data": {"repository": {"repo_full_name": "acme.co/images/payments"}}}
    assert [(j.project, j.kind) for j in app.jobs_from_harbor(default)] == [("payments", "chart")]
    assert [(j.project, j.kind) for j in app.jobs_from_harbor(cloud)] == [("payments", "image")]
    assert app.jobs_from_harbor({"type": "DELETE_ARTIFACT"}) == []
