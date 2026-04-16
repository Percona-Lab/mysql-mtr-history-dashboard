# MTR history dashboard — common tasks.
# Reqs: uv, just, `jenkins` Rust CLI on PATH, SSH to root@162.55.36.239 for ingest.
#
# Usage: `just <task>` — run `just -l` for the full list.

# --- defaults ---
instance       := "ps80"
job            := "percona-server-8.0-pipeline-parallel-mtr"
job_path       := instance + "/" + job
limit          := "200"
workers        := "4"
hetzner_host   := "root@162.55.36.239"
hetzner_stage  := "/opt/observability/backfill"

# Show all targets
default:
    @just --list

# One-time setup: create venv and install pinned deps via uv.
sync:
    uv sync

# Dev-mode install (includes pytest etc.)
sync-dev:
    uv sync --extra dev

# Upgrade + relock dependencies
lock:
    uv lock --upgrade

# --- stage A: jenkins → per-build JSON ---

# Fetch last N builds (default 200) as per-build JSON.
fetch n=limit:
    uv run mtr-backfill fetch {{job_path}} --limit {{n}} --workers {{workers}}

# Re-fetch even if JSONs already exist (use after schema bump).
fetch-force n=limit:
    uv run mtr-backfill fetch {{job_path}} --limit {{n}} --workers {{workers}} --force

# Fetch one specific build by number.
fetch-one build:
    uv run mtr-backfill fetch-one {{job_path}} -b {{build}}

# Summarise the builds/ directory.
status:
    uv run mtr-backfill status

# --- stage B: per-build JSON → OpenMetrics ---

# Emit one openmetrics file per build JSON.
export:
    uv run mtr-backfill export --json-dir builds --out-dir promtool/by-build

# Merge per-build files into a single sorted openmetrics file ready for promtool.
merge:
    uv run mtr-backfill merge --in-dir promtool/by-build --out promtool/merged.openmetrics.txt

# Export + merge in one shot.
build: export merge

# --- stage C: ingest into Prometheus on Hetzner host ---

# Dry-run the ingest pipeline (rsync --dry-run, no promtool invocation).
ingest-dry:
    ./bin/mtr-ingest --host {{hetzner_host}} --stage-dir {{hetzner_stage}} --dry-run

# Upload merged.openmetrics.txt, run promtool, reload Prometheus.
ingest:
    ./bin/mtr-ingest --host {{hetzner_host}} --stage-dir {{hetzner_stage}}

# --- full pipelines ---

# Fetch → export → merge → ingest.  Idempotent: re-running skips up-to-date JSONs.
all n=limit:
    @just fetch {{n}}
    @just build
    @just ingest

# --- tests ---

test:
    uv run --extra dev pytest

test-cov:
    uv run --extra dev pytest --cov=mtr_history --cov-report=term-missing

# --- maintenance ---

# Remove generated JSONs and openmetrics files.  Jenkins is the source of truth.
clean:
    rm -rf builds/*.json promtool/by-build/*.openmetrics.txt promtool/merged.openmetrics.txt
    find builds -type d -name xml -exec rm -rf {} +

# Remove the uv venv.
clean-venv:
    rm -rf .venv

# --- dashboard provisioning ---

# Push the dashboard JSON + provisioning YAML to the Hetzner host.
# The target compose.yml must mount:
#   ./grafana/provisioning/dashboards/mtr-dashboards.yml → /etc/grafana/provisioning/dashboards/mtr-dashboards.yml
#   ./grafana/dashboards/ → /var/lib/grafana/dashboards/mtr-history/
deploy-dashboard:
    ssh {{hetzner_host}} "mkdir -p /opt/observability/grafana/provisioning/dashboards /opt/observability/grafana/dashboards"
    rsync -av grafana/dashboards/                  {{hetzner_host}}:/opt/observability/grafana/dashboards/
    rsync -av grafana/provisioning/dashboards/     {{hetzner_host}}:/opt/observability/grafana/provisioning/dashboards/
    rsync -av grafana/compose.override.yml         {{hetzner_host}}:/opt/observability/compose.override.yml
    ssh {{hetzner_host}} "cd /opt/observability && docker compose up -d grafana"

# Fast iterative push: copy dashboard JSON only, relies on Grafana's file provisioner
# (updateIntervalSeconds=30 in mtr-dashboards.yml) to pick up the change within 30s.
push-dashboard:
    @jq empty grafana/dashboards/mtr-history.json && echo "JSON valid"
    rsync -a grafana/dashboards/ {{hetzner_host}}:/opt/observability/grafana/dashboards/
    @echo "pushed; Grafana will reload within 30s"
