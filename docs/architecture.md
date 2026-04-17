# Architecture

## System overview

```mermaid
graph LR
    J[Jenkins ps80] -->|REST API| F[mtr-backfill fetch-rest]
    F -->|per-build JSON| E[mtr-backfill export + merge]
    E -->|OpenMetrics text| I[rsync + promtool]
    I -->|TSDB blocks| P[Prometheus]
    P -->|PromQL| G[Grafana]
    G -->|HTTPS| U((Users))

    style J fill:#f9d71c,color:#000
    style P fill:#e6522c,color:#fff
    style G fill:#ff9830,color:#000
```

## Pipeline stages

The pipeline runs as a Jenkins job (`mysql-mtr-history-dashboard` on ps80)
or locally via `just` recipes. Four stages, each idempotent:

```mermaid
flowchart TD
    subgraph "Stage 1: Setup"
        S1[git clone + uv sync]
    end

    subgraph "Stage 2: Fetch"
        S2A[Query Prometheus for\nalready-ingested build_n values]
        S2B[mtr-backfill fetch-rest\n--skip-builds ...]
        S2C[Per-build: REST API\n+ JUnit XML download\n+ parse + validate]
        S2D[builds/*.json]
        S2A --> S2B --> S2C --> S2D
    end

    subgraph "Stage 3: Export"
        S3A[mtr-backfill export\nJSON -> OpenMetrics per build]
        S3B[mtr-backfill merge\nper-build files -> single sorted file]
        S3C[promtool/merged.openmetrics.txt]
        S3A --> S3B --> S3C
    end

    subgraph "Stage 4: Ingest"
        S4A[rsync to Hetzner host]
        S4B[promtool tsdb\ncreate-blocks-from openmetrics]
        S4C[POST /-/reload]
        S4D[Verification query]
        S4A --> S4B --> S4C --> S4D
    end

    S1 --> S2A
    S2D --> S3A
    S3C --> S4A
```

## Python module dependency graph

```mermaid
graph TD
    CLI["backfill.py\n(Click CLI entry point)"]
    B2J["build_to_json.py\n(orchestrator)"]
    JF["jenkins_fetcher.py\n(Rust CLI wrapper)"]
    JRF["jenkins_rest_fetcher.py\n(stdlib urllib)"]
    JP["junit_parser.py\n(XML parsing)"]
    SCH["schema.py\n(Pydantic models)"]
    OME["openmetrics_exporter.py\n(metrics generation)"]

    CLI --> B2J
    CLI --> JF
    CLI --> JRF
    CLI --> OME
    CLI --> SCH

    B2J --> JF
    B2J --> JRF
    B2J --> JP
    B2J --> SCH

    OME --> SCH

    style JF fill:#ccc,stroke:#999,stroke-dasharray: 5 5
    style JRF fill:#d4edda
```

**Solid boxes** = production path (Jenkins pipeline).
**Dashed box** (`jenkins_fetcher.py`) = local dev only (requires Rust `jenkins` binary).

## Data transformation pipeline

```mermaid
flowchart LR
    subgraph Jenkins REST API
        A1["/job/{job}/api/json\n(build list)"]
        A2["/job/{job}/{n}/api/json\n(build detail)"]
        A3["/job/{job}/{n}/artifact/\njunit_*.xml"]
    end

    subgraph "Per-build processing (4 parallel workers)"
        B1["fetch_detail()"]
        B2["download_junit_xmls()\n19 XMLs per build"]
        B3["parse_all_junit_files()\nmerge regular + big"]
        B4["Build (Pydantic v2)\nvalidate + serialize"]
    end

    subgraph "OpenMetrics export"
        C1["iter_samples()\n5 metric families"]
        C2["write per-build\n.openmetrics.txt"]
        C3["merge into single\nsorted file"]
    end

    A1 --> B1
    A2 --> B1
    A3 --> B2
    B1 --> B4
    B2 --> B3
    B3 --> B4
    B4 --> C1
    C1 --> C2
    C2 --> C3
```

## Prometheus metrics

Five metric families emitted per build:

```mermaid
graph TD
    subgraph "Per build (1 sample each)"
        M1["mtr_build_info\nresult, cause, build_url\nvalue = 1"]
        M2["mtr_build_summary_total\n3 samples: pass, fail, skip\nvalue = count"]
    end

    subgraph "Per test (~21K per build)"
        M3["mtr_test_status\nvalue = 1 PASS / 0 FAIL / 2 SKIP"]
        M4["mtr_test_duration_seconds\nvalue = seconds"]
    end

    subgraph "Per failed test only"
        M5["mtr_test_failure_info\n+ failure_msg label (200 char)\nvalue = 1"]
    end
```

Common labels across all metrics:
`instance`, `job_name`, `build` (zero-padded), `build_n` (unpadded),
`platform_os`, `platform_build`, `arch`, `branch`, `fork`.

Test-level metrics add: `suite`, `testname`, `run_context`, `worker_slug`, `worker_num`.

All samples are timestamped at **build start time** (historical, not wall clock).

## JUnit XML parsing

19 XML files per full MTR build:

```mermaid
graph LR
    subgraph "8 regular workers"
        W["junit_WORKER_{1..8}.xml"]
    end
    subgraph "8 big-test workers"
        B["junit_WORKER_{1..8}-big.xml"]
    end
    subgraph "3 special contexts"
        S1["junit_ci_fs.xml"]
        S2["junit_ps_protocol.xml"]
        S3["junit_UNIT_TESTS.xml"]
    end

    W --> P["parse_all_junit_files()"]
    B --> P
    S1 --> P
    S2 --> P
    S3 --> P

    P --> M["merge_test_records()\ndeduplicate by identity"]
    M --> R["~21,459 RawTestRecord"]
```

**Test identity:** `(suite, name, run_context)` -- worker and big are
attributes, not identity. Regular and big are NOT merged across each other.

**Merge rule:** On exact-identity duplicates (same suite/name/context/worker/big),
the non-skip record wins.

## Infrastructure topology

```mermaid
graph TD
    subgraph "Internet"
        U((Users))
    end

    subgraph "Hetzner host 162.55.36.239"
        subgraph "Docker bridge network: obs"
            CADDY["Caddy 2-alpine\n:80 :443 (public)"]
            GRAF["Grafana 11.6.0\n:3000 (loopback)"]
            PROM["Prometheus v3.11.2\n:9090 (loopback)\n180d retention"]
            PGW["Pushgateway v1.10.0\n:9091 (loopback)"]
            NE["Node Exporter v1.9.1\n:9100 (loopback)"]
        end

        subgraph "Volumes"
            V1["prom-data"]
            V2["grafana-data"]
            V3["pushgateway-data"]
        end
    end

    U -->|HTTPS| CADDY
    CADDY -->|"mtr-dashboard.cd.percona.com"| GRAF
    CADDY -->|"prom-mtr.cd.percona.com"| PROM
    GRAF -->|PromQL| PROM
    PROM --> V1
    GRAF --> V2
    PGW --> V3
    PROM -->|scrape 15s| NE
    PROM -->|scrape 15s| PGW
    PROM -->|scrape 15s| PROM
```

All services use `restart: unless-stopped`. Only Caddy binds to `0.0.0.0`;
all other services bind to `127.0.0.1`.

## Dashboard provisioning

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant Host as Hetzner host
    participant Grafana

    Dev->>Host: just push-dashboard<br/>(rsync grafana/dashboards/)
    Note over Host: /opt/observability/grafana/dashboards/<br/>mtr-history.json updated on disk
    Host->>Grafana: File provisioner detects change<br/>(updateIntervalSeconds=30)
    Grafana->>Grafana: Reload dashboard from JSON
    Note over Grafana: Dashboard live within ~30s
```

For full deploys (provisioning YAML + compose override + dashboards):
```bash
just deploy-dashboard
```

## Idempotency mechanisms

```mermaid
flowchart TD
    A["Build N candidate"] --> B{"JSON exists with\ncurrent schema_version?"}
    B -->|Yes| C[Skip]
    B -->|No| D{"build_n in\n--skip-builds?"}
    D -->|Yes| C
    D -->|No| E[Fetch + process]
    E --> F{"Fetch error?"}
    F -->|Yes| G["Write tombstone JSON\nresult=FETCH_ERROR"]
    F -->|No| H["Write build JSON\nresult=UNSTABLE|FAILURE"]
```

Three layers of idempotency:
1. **Schema version check** -- `build_to_json` skips if JSON exists with `schema_version == SCHEMA_VERSION`
2. **Prometheus pre-query** -- Jenkins pipeline queries for already-ingested `build_n` values, passes as `--skip-builds`
3. **`--force` flag** -- overrides all checks for re-processing

Tombstone JSONs (`result=FETCH_ERROR`) prevent retrying permanently broken builds
on every run while preserving the error context in `backfill_meta.error`.

## Error handling

| Layer | Strategy |
|-------|----------|
| Per-build fetch | Catch all exceptions, write tombstone JSON, continue with next build |
| JUnit XML parsing | Catch `ParseError`/`OSError` per file, add to warnings, continue |
| ThreadPoolExecutor | 4 parallel workers, individual failures don't kill the batch |
| Pipeline summary | Classify results as processed/skipped/tombstone/errored, print counts |

## Two fetcher backends

```mermaid
graph TD
    subgraph "Production path (Jenkins pipeline)"
        REST["jenkins_rest_fetcher.py\nstdlib urllib\nno external binary"]
    end

    subgraph "Local dev path (just fetch)"
        RUST["jenkins_fetcher.py\nRust jenkins CLI\nsubprocess wrapper"]
    end

    REST --> B2J["build_to_json.py\nprocess_build_rest()"]
    RUST --> B2J2["build_to_json.py\nprocess_build()"]
    B2J --> OUT["Identical JSON output"]
    B2J2 --> OUT

    style REST fill:#d4edda
    style RUST fill:#ccc,stroke:#999,stroke-dasharray: 5 5
```

Both backends produce **byte-identical** output (cross-verified on build 1558:
21,459 tests, exact match). The REST path is used in production because the
Jenkins pipeline agent doesn't have the Rust `jenkins` binary installed.
