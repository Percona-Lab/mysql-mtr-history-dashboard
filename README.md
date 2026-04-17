# mysql-mtr-history-dashboard

Per-build test failure history for `percona-server-8.0-pipeline-parallel-mtr`
on ps80, backed by Prometheus, visualised in Grafana.
Only **UNSTABLE/FAILURE** builds are ingested.

## Public URLs

| Service | URL |
|---------|-----|
| Grafana dashboard | <https://mtr-dashboard.cd.percona.com/d/mtr-history/mysql-mtr-history> |
| Prometheus | <https://prom-mtr.cd.percona.com> (read-only) |
| Jenkins job | <https://ps80.cd.percona.com/job/mysql-mtr-history-dashboard/> |

## Data flow

```
Jenkins pipeline (ps80/mysql-mtr-history-dashboard)
  │
  ├─ Fetch ──► REST API (stdlib urllib) ──► builds/<instance>_<job>_<N>.json
  │              per-build JSON with platform, tests[], summary, failure_message
  │
  ├─ Export ─► openmetrics_exporter.py ──► promtool/by-build/<NNNNN>.openmetrics.txt
  │              one file per build, 5 metric families, real build-start timestamps
  │
  ├─ Merge ──► promtool/merged.openmetrics.txt
  │              single sorted file, HELP/TYPE headers deduplicated
  │
  └─ Ingest ─► rsync to Hetzner ──► promtool tsdb create-blocks-from ──► Prometheus ──► Grafana
                 curl /-/reload        dashboards provisioned from this repo
```

## Layout

| Path | Purpose |
|------|---------|
| `mtr_history/` | Python package (click CLI + pydantic models, `uv`-managed) |
| `mtr_history/tests/` | 60 pytest cases (junit parser, REST fetcher, OpenMetrics exporter) |
| `bin/mtr-ingest` | Shell script for local ingest (rsync + promtool + reload) |
| `builds/` | Generated per-build JSONs (gitignored) |
| `promtool/` | Generated OpenMetrics text (gitignored) |
| `grafana/dashboards/` | Dashboard JSON, provisioned via file provisioner |
| `grafana/provisioning/` | Grafana provisioning YAML |
| `grafana/compose.override.yml` | Docker Compose override for Grafana volume mounts |
| `compose.yml` | Docker Compose stack (Prometheus, Grafana, Caddy, Pushgateway, Node Exporter) |
| `caddy/Caddyfile` | Caddy reverse proxy + TLS config |
| `Jenkinsfile` | Pipeline definition (4 stages: Setup, Fetch, Export, Ingest) |
| `jenkins-job.yaml` | Job config for `jenkins job create/update` |
| `justfile` | Local task runner (25 recipes) |
| `pyproject.toml` | Python >=3.11, deps: click + pydantic, entry point: `mtr-backfill` |

## Dashboard

Single panel: **Failure matrix -- tests x builds** (table).

Columns: Build, Suite, Test, Worker, Branch, Error.
Sorted by Build desc. Build and Test columns link to Jenkins.

Default time range: `now-90d`. Samples are timestamped at **build start time**
(sparse historical events), so shorter ranges go blank as builds age.

Filters (all default to **All**):
OS, Build Type, Arch, Branch, Fork, Suite, Test.

Deploy changes:
```bash
just push-dashboard      # rsync JSON, Grafana picks up within 30s
just deploy-dashboard    # full deploy: dashboards + provisioning + compose override
```

## Jenkins pipeline job

**Job:** `mysql-mtr-history-dashboard` on ps80 (manually triggered).

| Parameter | Default | Description |
|-----------|---------|-------------|
| `LIMIT` | 20 | Recent builds to scan |
| `RESULT_FILTER` | `UNSTABLE,FAILURE` | Which build results to process |
| `FORCE` | false | Re-process already-ingested builds |
| `DRY_RUN` | false | Generate OpenMetrics but skip ingest |

**Stages:**
1. **Setup** -- clone repo, `uv sync`
2. **Fetch** -- query Prometheus for already-ingested builds, `mtr-backfill fetch-rest` for the rest
3. **Export** -- `mtr-backfill export` + `mtr-backfill merge` (JSON to OpenMetrics)
4. **Ingest** -- rsync to Hetzner, `promtool tsdb create-blocks-from openmetrics`, Prometheus reload

The pipeline is idempotent: without `FORCE=true`, re-running is a no-op if all
candidate builds are already in Prometheus.

**Trigger:**
```bash
jenkins build ps80/mysql-mtr-history-dashboard -p LIMIT=20 -p RESULT_FILTER=UNSTABLE,FAILURE
```

## CLI reference

Entry point: `uv run mtr-backfill <command>` (or `just <recipe>`).

| Command | Description | Used by |
|---------|-------------|---------|
| `fetch-rest` | Fetch builds via Jenkins REST API (no Rust CLI needed) | Jenkins pipeline, `just fetch-rest` |
| `fetch` | Fetch builds via Rust `jenkins` CLI | `just fetch` (local dev) |
| `fetch-one` | Fetch one build by number (debugging) | `just fetch-one` (local dev) |
| `export` | Emit one OpenMetrics file per build JSON | Jenkins pipeline, `just export` |
| `merge` | Merge per-build OpenMetrics into one sorted file | Jenkins pipeline, `just merge` |
| `rebuild` | Re-fetch JSONs with stale schema_version | `uv run mtr-backfill rebuild` |
| `status` | Summarise the builds/ directory | `just status` |

## Quickstart

```bash
# Install
just sync                # install deps via uv

# Fetch + ingest (REST API path, no external binary needed)
just fetch-rest          # backfill last 20 UNSTABLE/FAILURE builds
just build               # JSON -> OpenMetrics (export + merge)
just ingest              # rsync to Hetzner, promtool, reload Prometheus

# Fetch + ingest (Rust CLI path, requires `jenkins` binary)
just fetch               # backfill last 200 builds
just build && just ingest

# Test
just test                # pytest (60 cases)

# Dashboard
just push-dashboard      # push dashboard JSON to Grafana (30s reload)
```

## Per-build JSON schema (v1)

One file per build at `builds/<instance>_<job>_<N>.json`. Key fields:

- `build_number`, `url`, `timestamp` (ISO 8601 UTC), `duration_ms`, `result`
- `platform.{docker_os, arch, cmake_build_type}`
- `source.{fork, repo, branch, sha}` (sha always null in v1)
- `cause.{kind, user, description}`, `scm[]`, `parameters`
- `summary.{pass, fail, skip, total, duration_s}` -- computed from XMLs, not
  Jenkins (Jenkins caps failures at 500/suite)
- `tests[]` -- each test with `suite, name, run_context, worker, big, status, time_s, failure_message`
- `backfill_meta.{schema_version, backfill_ts, junit_xml_files, warnings}`

**Test identity** = `(suite, name, run_context)` where run_context is
`regular | ci_fs | ps_protocol | unit_tests`. This handles the 56 suite-name
collisions between regular/big worker runs and the special-context XMLs.

**Regular vs `-big` XML merge**: `junit_WORKER_N.xml` and `junit_WORKER_N-big.xml`
overlap; the one marked `<skipped>` is dropped in favor of the non-skipped run.

## Prometheus metrics

| Metric | Labels | Value | Emitted for |
|--------|--------|-------|-------------|
| `mtr_test_status` | instance, job_name, build, build_n, suite, testname, platform_os, platform_build, arch, branch, fork, run_context, worker_slug, worker_num | 1=PASS, 0=FAIL, 2=SKIP | every test |
| `mtr_test_duration_seconds` | same as above | seconds | every test |
| `mtr_test_failure_info` | same + `failure_msg` (200 char truncated) | 1.0 | **failed tests only** |
| `mtr_build_summary_total` | instance, job_name, build, build_n, platform_os, platform_build, arch, branch, fork, status | count | every build |
| `mtr_build_info` | instance, job_name, build, build_n, platform_os, platform_build, arch, branch, fork, result, cause_kind, cause_user, build_url | 1 | every build |

- `build` = zero-padded 5 digits (`01558`) for sort; `build_n` = unpadded (`1558`) for Jenkins URLs
- `worker_slug` = `WORKER_2` or `WORKER_2-big` (or `ci_fs`/`ps_protocol`/`UNIT_TESTS`)
- Samples timestamped at **build start time** (historical, not wall clock)
- ~21,459 test records per full-MTR build, ~170K Prometheus samples per 5-build batch

## Infrastructure

Hetzner host `162.55.36.239`, stack at `/opt/observability/`.

| Service | Version | Port |
|---------|---------|------|
| Grafana | 11.6.0 | 3000 (via Caddy at 443) |
| Prometheus | v3.11.2 | 9090 (localhost only, via Caddy at 443) |
| Pushgateway | v1.10.0 | 9091 |
| Node Exporter | v1.9.1 | 9100 |
| Caddy | 2-alpine | 80, 443 |

- Prometheus retention: **180 days**
- 102 builds ingested across 26 platform combinations
- Dashboard provisioned via file provisioner (`updateIntervalSeconds=30`)
- TLS via Caddy with automatic HTTPS

## References

- Live reference build: <https://ps80.cd.percona.com/job/percona-server-8.0-pipeline-parallel-mtr/1558/>
- `ps-build` (pipeline + junit generation): <https://github.com/Percona-Lab/ps-build>
