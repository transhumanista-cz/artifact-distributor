# artifact-distributor

Distributes container images and Helm charts from a private Harbor to any external OCI registry —
automatically by pattern (Harbor webhook) or on demand — controlled by a `.distribution.yaml` in
each project repository (GitOps). Credentials are resolved from Vault/OpenBao per registry host +
namespace prefix; every component of the reference (host, prefix, name, tag) can be renamed.

Design, alternatives and trade-offs: **[docs/DESIGN.md](docs/DESIGN.md)**.

## Quick start (docker compose PoC)

```bash
docker compose up -d --build      # 3 registries, OpenBao, distributor
docker compose run --rm demo      # end-to-end scenario with assertions
docker compose down -v            # the demo expects fresh registries; reset before re-running
```

| Service | Host port | Role |
|---|---|---|
| `harbor` | 5001 | stand-in for Harbor (zot, same `acme.co/images|charts/<project>` layout) |
| `customer` | 5002 | customer registry with **one account per namespace** (`vendor/acme`, `sandbox/acme`) |
| `public` | 5003 | stand-in for the public Azure registry (anonymous pull) |
| `openbao` | 8200 | secret store, UI token `root` |
| `distributor` | 8080 | the service |

The demo plays CI + Harbor (pushes multi-arch images and real Helm charts, fires Harbor-format
`PUSH_ARTIFACT` webhooks) and checks:

```
[1] webhooks trigger automatic, pattern-based distribution (image+chart, image-only, chart-only)
[2] renaming of single components and full references
[3] credentials per host + namespace (two accounts on the same customer registry)
[4] cost control: multi-arch kept / single platform copied, keepLatest + prune on a new release
[5] on-demand distribution of a manual rule through the API
[6] GitOps: editing a .distribution.yaml distributes on its own
[7] policy denial and rejected configs are reported
[8] a full reconcile afterwards copies nothing (idempotent)
ALL CHECKS PASSED
```

Without Docker: `make e2e-local` runs the identical scenario with the `zot`, `bao` and `regctl`
binaries (this is how it was verified while writing it). Unit tests: `make test`.

Poke around after `docker compose up`:

```bash
docker compose run --rm --entrypoint python demo poc/tools/seed.py scenario initial   # push + webhooks
curl -s localhost:8080/api/v1/projects -H 'Authorization: Bearer poc-api-token'
curl -s 'localhost:8080/api/v1/plan?manual=true' -H 'Authorization: Bearer poc-api-token'
curl -s localhost:8080/api/v1/events -H 'Authorization: Bearer poc-api-token'
regctl tag ls --host reg=localhost:5003,tls=disabled localhost:5003/acme/images/payments
```

## For developers: `.distribution.yaml`

```yaml
apiVersion: distribution.acme.co/v1
rules:
  - name: public-stable                      # stable releases to the public registry
    artifacts: [image, chart]
    tags: {semver: ">=1.3.0", prerelease: exclude, keepLatest: 2}
    prune: true                              # older ones this rule pushed are removed
    to: [public]                             # target names come from the platform config

  - name: customer-x-field-test              # release candidates for in-field testing
    artifacts: [image]
    tags: {include: ['v(?P<ver>\d+\.\d+\.\d+)-rc\.(?P<rc>\d+)']}
    platform: linux/amd64
    to:
      - target: customer-x
        name: acme-${name}                   # rename only the name ...
        tag: {regex: '^v', replace: ''}      # ... and strip the "v" from the tag
      - target: customer-x-sandbox
        tag: ${ver}-fieldtest.${rc}

  - name: pilot                              # on demand only (API / CLI)
    trigger: manual
    to: [{target: public, prefix: "acme/pilot/${kind_dir}"}]
```

| Field | Meaning |
|---|---|
| `artifacts` | `image`, `chart` (default both; a project without one simply has nothing there) |
| `trigger` | `auto` (webhook + reconcile, default) or `manual` (only via `/api/v1/sync`) |
| `tags.include` / `exclude` | regexes (full match for include); named groups become variables |
| `tags.list` | explicit tags — the declarative "on demand" |
| `tags.semver` | `>=1.2 <2`, `^1.4`, `~1.4.2`, `1.x`, `a || b` |
| `tags.prerelease` | `include` (default), `exclude`, `only` |
| `tags.keepLatest` | newest N by SemVer |
| `prune` | delete destination tags this rule pushed that are no longer selected |
| `platform` | copy one platform (`linux/amd64`) out of a multi-arch image |
| `referrers` | also copy signatures/SBOMs |
| `to[]` | `target` and optional `registry` / `prefix` / `name` / `tag` overrides, or `ref` |

Override values are templates (`${name}`, `${tag}`, `${prefix}`, `${registry}`, `${project}`,
`${kind}`, `${kind_dir}`, `${target_registry}`, `${target_prefix}`, `${major}`, `${minor}`,
`${patch}`, `${prerelease}`, `${version}`, named regex groups) or `{regex, replace}` applied to the
source value. Editor completion: `schema/distribution.schema.json`.

PR check for project repos: `ci/github/caller-example.yml` → reusable workflow
`ci/github/distribution.yml` (validate offline; plan as PR comment from a VPN runner).

## API

| Method | Path | |
|---|---|---|
| POST | `/webhooks/harbor` | Harbor webhook (Default or CloudEvents payload), `Authorization: <WEBHOOK_SECRET>` |
| POST | `/api/v1/sync` | `{"project","rule"?,"tag"?,"kind"?,"dryRun"?}` on demand, includes manual rules |
| POST | `/api/v1/reconcile` | `{"project"?, "wait"?}` |
| POST | `/api/v1/reload` | re-read project configs now |
| GET | `/api/v1/plan` | `?project&rule&tag&manual=true` dry run with digest diff |
| GET | `/api/v1/projects` | loaded projects and **rejected configs with reasons** |
| GET | `/api/v1/events` | audit log |
| GET | `/metrics`, `/healthz` | Prometheus, liveness |

`/api/v1/*` requires `Authorization: Bearer <API_TOKEN>`.

## CLI

```
python -m distributor --platform platform.yaml validate [--file .distribution.yaml --repo NAME]
python -m distributor plan [--project P] [--rule R] [--tag T] [--manual] [--json]
python -m distributor reconcile [--project P]
python -m distributor sync --project P [--rule R] [--tag T]
python -m distributor creds cr.customer-x.com/vendor/acme/images/x   # which secret key resolves
python -m distributor serve
```

## Layout

```
distributor/        service (Python 3.11+, PyYAML only; copy engine: regctl)
  config.py         platform + project config model and validation
  selector.py       tag selection (regex, SemVer, prerelease, keepLatest)    semver.py
  rename.py         per-component renaming                                     refs.py
  secrets.py        credential resolution by host + prefix (Vault/OpenBao, file, env)
  engine.py         regctl wrapper (per-call private credentials)
  core.py           planner (policy, collisions, prune) + executor (digest diff, verify)
  gitops.py         config sources: dir, git, GitHub org scan
  server.py         webhooks, API, GitOps poller, periodic reconcile        cli.py
poc/                platform.yaml, registries (zot), OpenBao seed, demo repos, seeder, run-local.sh
poc/harbor/         wiring a real Harbor (robot account + webhook policy)
ci/github/          reusable PR workflow for project repos
schema/             JSON schema for .distribution.yaml
tests/              unit tests, end-to-end scenario
docs/DESIGN.md      design document
```
