"""`mtr-backfill` CLI: fetch, export, merge, rebuild, status."""

from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click

from . import build_to_json, jenkins_fetcher, openmetrics_exporter
from .schema import SCHEMA_VERSION


def _parse_job_path(job_path: str) -> tuple[str, str]:
    if "/" not in job_path:
        raise click.BadParameter(
            f"expected <instance>/<job>, got {job_path!r}", param_hint="JOB_PATH"
        )
    inst, job = job_path.split("/", 1)
    return inst, job


@click.group()
def cli() -> None:
    """MTR history: Jenkins → per-build JSON → Prometheus → Grafana."""


# --- fetch ------------------------------------------------------------------

@cli.command()
@click.argument("job_path")
@click.option("--limit", default=200, show_default=True, help="How many recent builds to consider.")
@click.option("--workers", default=4, show_default=True, help="Parallel backfill workers.")
@click.option("--force", is_flag=True, help="Re-fetch even if JSON already exists.")
@click.option("--keep-xml", is_flag=True, help="Keep junit XMLs under builds/xml/.")
@click.option("--builds-dir", type=click.Path(path_type=Path), default=Path("builds"), show_default=True)
def fetch(
    job_path: str,
    limit: int,
    workers: int,
    force: bool,
    keep_xml: bool,
    builds_dir: Path,
) -> None:
    """Backfill the last N builds of JOB_PATH into builds/*.json."""
    instance, job = _parse_job_path(job_path)
    click.echo(f"→ listing {limit} most-recent builds of {job_path} …")
    history = jenkins_fetcher.fetch_history(instance, job, limit)
    click.echo(f"  found {len(history)} builds")

    if not history:
        click.echo("nothing to do"); return

    processed = 0
    skipped = 0
    tombstoned = 0
    errored = 0

    def _do(entry: dict) -> tuple[int, str, str | None]:
        n = int(entry["number"])
        try:
            out = build_to_json.process_build(
                instance=instance,
                job=job,
                build_number=n,
                builds_dir=builds_dir,
                force=force,
                keep_xml=keep_xml,
                history_entry=entry,
            )
        except Exception as e:  # noqa: BLE001
            return n, "error", str(e)
        if out is None:
            return n, "skipped", None
        # Peek result to flag tombstones.
        try:
            data = json.loads(out.read_text())
            if data.get("result") == "FETCH_ERROR":
                return n, "tombstone", data.get("backfill_meta", {}).get("error")
        except Exception:  # noqa: BLE001
            pass
        return n, "processed", None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_do, entry) for entry in history]
        for fut in as_completed(futs):
            n, status, err = fut.result()
            if status == "processed":
                processed += 1
                click.echo(f"  ✓ {n}")
            elif status == "skipped":
                skipped += 1
            elif status == "tombstone":
                tombstoned += 1
                click.echo(f"  ⚠ {n} tombstone: {err}")
            else:
                errored += 1
                click.echo(f"  ✗ {n} error: {err}", err=True)

    click.echo(
        f"\ndone — processed={processed} skipped={skipped} "
        f"tombstoned={tombstoned} errored={errored}"
    )


# --- fetch-one --------------------------------------------------------------

@cli.command("fetch-one")
@click.argument("job_path")
@click.option("-b", "--build", "build_number", required=True, type=int)
@click.option("--force", is_flag=True)
@click.option("--keep-xml", is_flag=True)
@click.option("--builds-dir", type=click.Path(path_type=Path), default=Path("builds"))
def fetch_one(
    job_path: str,
    build_number: int,
    force: bool,
    keep_xml: bool,
    builds_dir: Path,
) -> None:
    """Backfill exactly one build by number (for debugging)."""
    instance, job = _parse_job_path(job_path)
    out = build_to_json.process_build(
        instance=instance,
        job=job,
        build_number=build_number,
        builds_dir=builds_dir,
        force=force,
        keep_xml=keep_xml,
    )
    if out is None:
        click.echo(f"skipped {build_number} (already up-to-date; pass --force to re-fetch)")
        sys.exit(0)
    click.echo(f"wrote {out}")


# --- status -----------------------------------------------------------------

@cli.command()
@click.option("--builds-dir", type=click.Path(path_type=Path), default=Path("builds"))
def status(builds_dir: Path) -> None:
    """Summarise the builds/ directory."""
    if not builds_dir.exists():
        click.echo("no builds/ dir"); return
    jsons = sorted(builds_dir.glob("*.json"))
    if not jsons:
        click.echo("no JSONs found"); return

    results: Counter[str] = Counter()
    platforms: Counter[str] = Counter()
    schemas: Counter[int] = Counter()
    branches: Counter[str] = Counter()
    build_numbers: list[int] = []
    total_tests = 0
    for p in jsons:
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        results[data.get("result", "?")] += 1
        plat = data.get("platform") or {}
        pkey = f"{plat.get('docker_os','?')}/{plat.get('cmake_build_type','?')}"
        platforms[pkey] += 1
        schemas[data.get("backfill_meta", {}).get("schema_version", 0)] += 1
        branches[(data.get("source") or {}).get("branch") or "?"] += 1
        build_numbers.append(int(data.get("build_number", 0)))
        total_tests += len(data.get("tests") or [])

    click.echo(f"Builds:    {len(jsons)} JSONs in {builds_dir}/")
    if build_numbers:
        click.echo(f"Range:     #{min(build_numbers)} .. #{max(build_numbers)}")
    click.echo(f"Results:   {dict(results)}")
    click.echo(f"Platforms: {dict(platforms.most_common(10))}")
    click.echo(f"Branches:  {dict(branches.most_common(10))}")
    click.echo(f"Schema:    {dict(schemas)}")
    click.echo(f"Tests ∑:   {total_tests:,}")


# --- rebuild ----------------------------------------------------------------

@cli.command()
@click.option("--schema-version", type=int, default=SCHEMA_VERSION, show_default=True,
              help="Target schema version; JSONs at < version are re-fetched.")
@click.option("--builds-dir", type=click.Path(path_type=Path), default=Path("builds"))
@click.option("--workers", default=4, show_default=True)
def rebuild(schema_version: int, builds_dir: Path, workers: int) -> None:
    """Re-fetch every JSON whose schema_version < target."""
    jsons = sorted(builds_dir.glob("*.json"))
    stale: list[Path] = []
    for p in jsons:
        try:
            data = json.loads(p.read_text())
        except Exception:
            stale.append(p); continue
        if data.get("backfill_meta", {}).get("schema_version", 0) < schema_version:
            stale.append(p)
    if not stale:
        click.echo("all JSONs at target schema"); return
    click.echo(f"re-fetching {len(stale)} JSONs (target schema v{schema_version})")
    # Parse out (instance, job, build) from filename: <inst>_<job>_<n>.json
    tasks: list[tuple[str, str, int]] = []
    for p in stale:
        stem = p.stem
        # job name itself may contain underscores — rsplit once.
        inst_job, n = stem.rsplit("_", 1)
        inst, job = inst_job.split("_", 1)
        tasks.append((inst, job, int(n)))

    def _do(t: tuple[str, str, int]) -> None:
        inst, job, n = t
        build_to_json.process_build(inst, job, n, builds_dir, force=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_do, tasks))
    click.echo("done")


# --- export + merge ---------------------------------------------------------

@cli.command()
@click.option("--json-dir", type=click.Path(path_type=Path), default=Path("builds"))
@click.option("--out-dir", type=click.Path(path_type=Path), default=Path("promtool/by-build"))
def export(json_dir: Path, out_dir: Path) -> None:
    """Emit one OpenMetrics file per per-build JSON."""
    report = openmetrics_exporter.build_openmetrics_bundle(json_dir, out_dir)
    click.echo(f"wrote {report['builds_processed']} files, {report['total_samples']:,} samples → {out_dir}")


@cli.command()
@click.option("--in-dir", type=click.Path(path_type=Path), default=Path("promtool/by-build"))
@click.option("--out", type=click.Path(path_type=Path), default=Path("promtool/merged.openmetrics.txt"))
def merge(in_dir: Path, out: Path) -> None:
    """Merge per-build OpenMetrics files into one sorted file for promtool."""
    n = openmetrics_exporter.merge_openmetrics_files(in_dir, out)
    click.echo(f"wrote {n:,} samples → {out}")


if __name__ == "__main__":
    cli()
