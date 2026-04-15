# mysql-mtr-history-dashboard

Per-build test history for `percona-server-8.0-pipeline-parallel-mtr` on ps80,
backed by Prometheus, visualised in Grafana.

## Data flow

```
Jenkins ps80 ──► builds/<N>.json ──► promtool/*.openmetrics.txt ──► Prometheus ──► Grafana
  (jenkins         (one per build,      (OpenMetrics text with        (via promtool       (dashboards
   CLI +            canonical            real build timestamps)        tsdb create-        provisioned
   junit XMLs)      artefact)                                          blocks-from         from this repo)
                                                                       openmetrics)
```

## Layout

| Path | Purpose |
|------|---------|
| `mtr_history/` | Python package (`uv`-managed) |
| `bin/` | Thin shell + uv-script wrappers (`mtr-ingest`) |
| `builds/` | Generated per-build JSONs (gitignored) |
| `promtool/` | Generated OpenMetrics text (gitignored) |
| `grafana/dashboards/` | Dashboard JSON files, committed |
| `grafana/provisioning/` | Provisioning YAML for Grafana, committed |
| `justfile` | Common tasks (`just sync`, `just fetch`, `just build`, `just ingest`, `just test`) |

## Per-build JSON schema (v1)

One file per build at `builds/<instance>_<job>_<N>.json`. Key fields:

- `build_number`, `url`, `timestamp` (ISO 8601 UTC), `duration_ms`, `result`, `display_name`
- `platform.{docker_os, arch, cmake_build_type}`
- `source.{fork, repo, branch, sha}` (sha is always null — the pipeline does
  not propagate the percona-server commit SHA)
- `cause.{kind, user, description}`
- `scm[]` — jenkins-pipelines + ps-build repos with their SHAs
- `parameters` — filtered map (empty values + per-worker suite overrides dropped)
- `summary.{pass, fail, skip, total, duration_s}` — computed from XMLs, not
  Jenkins (Jenkins caps failures at 500/suite)
- `tests[]` — each test with `suite, name, run_context, worker, big, status, time_s, failure_message`
- `backfill_meta.{schema_version, backfill_ts, jenkins_cli_version, junit_xml_files, warnings}`

**Test identity** = `(suite, name, run_context)` where run_context ∈
`{regular, ci_fs, ps_protocol, unit_tests}`. This handles the 56 suite-name
collisions between regular/big worker runs and the special-context XMLs
(e.g. `main.dd_is_compatibility_ci` appears in both regular and ci_fs).

**Regular vs `-big` XML merge**: `junit_WORKER_N.xml` and `junit_WORKER_N-big.xml`
overlap; the one marked `<skipped>` is dropped in favor of the non-skipped run.

## Prometheus metrics

| Metric | Labels | Value |
|--------|--------|-------|
| `mtr_test_status` | `instance, job_name, build, suite, testname, platform_os, platform_build, arch, branch, fork, run_context, worker_slug, worker_num` | `1=PASS / 0=FAIL / 2=SKIP` |
| `mtr_test_duration_seconds` | same as above | seconds |
| `mtr_build_summary_total` | `instance, job_name, build, platform_os, platform_build, arch, branch, fork, status` | count |
| `mtr_build_info` | `instance, job_name, build, …, result, cause_kind, cause_user, build_url` | `1` |

- `build` is zero-padded to 5 digits for correct alphabetic sort
- `worker_slug` = `WORKER_2` or `WORKER_2-big` (or `ci_fs / ps_protocol / UNIT_TESTS` for special contexts)
- `worker_num` = numeric suffix (empty for special contexts); used by the artifact deep-link URL template
- Timestamps are the Jenkins build start time (ISO → Unix seconds), not wall-clock
- Enum-as-value keeps cardinality at 1 series per test × build (~27K series/build, ~5.5M for 200 builds, ~50–100 MB TSDB disk)

## Quickstart

```bash
just sync              # install deps via uv
just fetch             # backfill last 200 builds → builds/*.json
just build             # JSON → OpenMetrics (per-build + merged)
just ingest            # rsync merged.openmetrics.txt → promtool on Hetzner host → /-/reload
just test              # pytest
```

## Hetzner host

Grafana + Prometheus + Pushgateway at `162.55.36.239` via
`/opt/observability/compose.yml` (outside this repo). Dashboard is
provisioned by pushing JSON from `grafana/dashboards/` to the host and
reloading Grafana (`just deploy-dashboard`). Admin login on the host is
managed separately.

## References

- Live reference build: <https://ps80.cd.percona.com/job/percona-server-8.0-pipeline-parallel-mtr/1558/>
- Upstream `jenkins` CLI: <https://github.com/percona/infra> (Rust, `~/.local/bin/jenkins`)
- `ps-build` (pipeline + junit generation): <https://github.com/Percona-Lab/ps-build>
