#!/usr/bin/env python3
import argparse
import gzip
import json
import re
import subprocess
from pathlib import Path
import sys
import tempfile
import zipfile


def build_jenkins_url(job_base_url: str, build_number: int) -> str:
    return f"{job_base_url.rstrip('/')}/{build_number}/api/json"


def build_artifact_zip_url(job_base_url: str, build_number: int) -> str:
    return f"{job_base_url.rstrip('/')}/{build_number}/artifact/*zip*/archive.zip"


def build_jenkins_build_url(job_base_url: str, build_number: int) -> str:
    return f"{job_base_url.rstrip('/')}/{build_number}/"


def curl_get(
    url: str, username: str | None = None, token: str | None = None, binary: bool = False
):
    cmd = ["curl", "-fsSL"]

    if username is not None and token is not None:
        cmd.extend(["-u", f"{username}:{token}"])
    elif username is not None or token is not None:
        print("Error: provide both --username and --token, or neither.", file=sys.stderr)
        raise SystemExit(1)

    try:
        result = subprocess.run(
            cmd + [url],
            check=True,
            capture_output=True,
            text=not binary,
        )
    except FileNotFoundError:
        print("Error: curl is not installed or not available in PATH", file=sys.stderr)
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error: curl failed for URL: {url}", file=sys.stderr)
        if e.stderr:
            print(e.stderr.strip(), file=sys.stderr)
        raise SystemExit(1)

    return result.stdout


def fetch_json_from_url(url: str, username: str | None = None, token: str | None = None) -> dict:
    text = curl_get(url, username, token, binary=False)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Error: response is not valid JSON: {e}", file=sys.stderr)
        raise SystemExit(1)


def run_failed_tests_extractor(extract_root: Path) -> dict[str, object]:
    extractor_script = Path(__file__).with_name("extract_failed_testsuites.py")
    if not extractor_script.exists():
        print(
            f"Warning: extractor script not found: {extractor_script}",
            file=sys.stderr,
        )
        return {"failed_tests": []}

    xml_files = sorted(str(p) for p in extract_root.rglob("junit_WORKER*.xml"))
    if not xml_files:
        return {"failed_tests": []}

    output_dir = extract_root / "archive" / "work" / "results"
    output_json = (
        output_dir / "failed_testsuites_all.json"
        if output_dir.exists()
        else extract_root / "failed_testsuites_all.json"
    )

    cmd = [
        sys.executable,
        str(extractor_script),
        *xml_files,
        "--json",
        "-o",
        str(output_json),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print("Warning: failed to run extract_failed_testsuites.py", file=sys.stderr)
        if e.stderr:
            print(e.stderr.strip(), file=sys.stderr)
        return {"failed_tests": []}

    try:
        return json.loads(output_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Warning: failed to read generated failed-tests JSON: {e}", file=sys.stderr)
        return {"failed_tests": []}


def extract_log_metadata(lines) -> tuple[str | None, str | None]:
    revision = None
    mysql_version = None

    for line in lines:
        if revision is None:
            m_rev = re.search(r"(?:REVISION|Revision)=([^\s]+)", line)
            if m_rev:
                revision = m_rev.group(1)
        if mysql_version is None:
            m_mysql = re.search(r"--\s*MySQL\s+(.+?)\s*$", line)
            if m_mysql:
                mysql_version = m_mysql.group(1).strip()
        if revision is not None and mysql_version is not None:
            break

    return revision, mysql_version


def extract_revision_from_artifact_zip(
    artifact_zip_path: Path,
) -> tuple[str | None, str | None, dict[str, object]]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with zipfile.ZipFile(artifact_zip_path, "r") as zf:
            zf.extractall(tmpdir)

            build_log_path = None
            build_log_gz_path = None

            for member in zf.namelist():
                normalized = member.replace("\\", "/")
                if normalized.endswith("/build.log"):
                    build_log_path = f"{tmpdir}/{member}"
                    break
                if normalized.endswith("/build.log.gz"):
                    build_log_gz_path = f"{tmpdir}/{member}"

        revision = None
        mysql_version = None

        if build_log_path:
            with open(build_log_path, "r", encoding="utf-8", errors="ignore") as f:
                revision, mysql_version = extract_log_metadata(f)
        elif build_log_gz_path:
            with gzip.open(build_log_gz_path, "rt", encoding="utf-8", errors="ignore") as f:
                revision, mysql_version = extract_log_metadata(f)

        failed_tests_json = run_failed_tests_extractor(tmpdir_path)
        return revision, mysql_version, failed_tests_json


def extract_jenkins_parameters(payload: dict) -> dict:
    params = {}

    for action in payload.get("actions", []):
        if not isinstance(action, dict):
            continue

        for p in action.get("parameters", []):
            if not isinstance(p, dict):
                continue

            name = p.get("name")
            if name is None:
                continue

            params[name] = p.get("value")

    return params


def get_build_result(payload: dict) -> str | None:
    value = payload.get("result")
    if isinstance(value, str):
        return value.upper()
    return None


def debug_non_success_result(payload: dict, build_url: str) -> str:
    result = payload.get("result")
    building = payload.get("building")
    in_progress = payload.get("inProgress")
    return (
        f"Build is not in allowed states (SUCCESS/UNSTABLE). "
        f"result={result!r}, building={building}, "
        f"inProgress={in_progress}, build_url={build_url}"
    )


def download_artifact_zip(
    artifact_zip_url: str,
    build_number: int,
    username: str | None = None,
    token: str | None = None,
) -> Path:
    zip_path = Path.cwd() / f"{build_number}.zip"

    # Reuse local zip for future runs if it already exists and is valid.
    if zip_path.exists():
        if zipfile.is_zipfile(zip_path):
            return zip_path
        print(
            f"Warning: existing file is not a valid zip, re-downloading: {zip_path}",
            file=sys.stderr,
        )

    zip_bytes = curl_get(artifact_zip_url, username, token, binary=True)

    try:
        zip_path.write_bytes(zip_bytes)
    except OSError as e:
        print(f"Error: failed to write zip file {zip_path}: {e}", file=sys.stderr)
        raise SystemExit(1)

    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Jenkins build params, extract Revision from artifact build.log, and output JSON."
    )
    parser.add_argument(
        "--build-number",
        type=int,
        required=True,
        help="Jenkins build number (example: 1559)",
    )
    parser.add_argument(
        "--job-base-url",
        default="https://ps80.cd.percona.com/job/percona-server-8.x-pipeline-parallel-mtr",
        help="Base Jenkins job URL without build number and without /api/json",
    )
    parser.add_argument(
        "--username",
        help="Jenkins username for basic auth",
        default=None,
    )
    parser.add_argument(
        "--token",
        help="Jenkins API token or password for basic auth",
        default=None,
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Optional output file path (default: <build_number>.json)",
        default=None,
    )
    args = parser.parse_args()

    jenkins_json_url = build_jenkins_url(args.job_base_url, args.build_number)
    artifact_zip_url = build_artifact_zip_url(args.job_base_url, args.build_number)
    build_url = build_jenkins_build_url(args.job_base_url, args.build_number)

    # 1) Check build status first and stop early unless SUCCESS or UNSTABLE.
    payload = fetch_json_from_url(jenkins_json_url, args.username, args.token)
    build_result = get_build_result(payload)
    allowed_results = {"SUCCESS", "UNSTABLE"}
    if build_result not in allowed_results:
        print(debug_non_success_result(payload, build_url), file=sys.stderr)
        # Graceful failure: no output JSON generated when build is not allowed.
        return 1

    # 2) Only allowed builds continue to artifact processing/output generation.
    result = extract_jenkins_parameters(payload)

    artifact_zip_path = download_artifact_zip(
        artifact_zip_url,
        args.build_number,
        args.username,
        args.token,
    )
    revision, mysql_version, failed_tests_json = extract_revision_from_artifact_zip(
        artifact_zip_path
    )

    # Keep these keys at the top of final JSON output.
    final_result: dict[str, object] = {
        "BUILD_NUMBER": args.build_number,
        "BUILD_URL": build_url,
        "BUILD_RESULT": build_result,
        "REVISION": revision,
        "MySQL_VERSION": mysql_version,
    }

    # Preserve Jenkins parameter order after the top metadata keys.
    for key, value in result.items():
        if key not in final_result:
            final_result[key] = value

    final_result.update(failed_tests_json)

    output_text = json.dumps(final_result, indent=2, ensure_ascii=False)

    output_path = args.output or f"{args.build_number}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output_text + "\n")

    print(f"Wrote output JSON to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
