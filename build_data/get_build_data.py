#!/usr/bin/env python3

"""
Fetch build parameters from a Jenkins build.

Auth: pass --user/--token, or set JENKINS_USER / JENKINS_TOKEN env vars.
API tokens are created at <JENKINS_URL>/user/<you>/configure -> "API Token".

Usage:
export JENKINS_USER=myuser
export JENKINS_TOKEN=11aabbcc...
./get_build_data.py
./get_build_data.py --build lastBuild --format env
./get_build_data.py --url https://ps80.cd.percona.com \\
                            --job percona-server-8.0-pipeline-parallel-mtr \\
                            --build 1559 --format table
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote
import httpx
from pathlib import Path
from typing import Optional


# ---- Defaults (override with CLI flags) --------------------------------------
JENKINS_URL = "https://ps80.cd.percona.com"
JOB_NAME    = "percona-server-8.0-pipeline-parallel-mtr"
# -----------------------------------------------------------------------------


def extract_from_line(path: str | Path, suffix = "/binary.tar.gz") -> Optional[str]:
    path = Path(path)
    
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.endswith(suffix):
                full_path = line[:-len(suffix)]
                return full_path.rsplit("/", 1)[-1]

    return None

def get_one_info(path: str, prefix: str) -> str | None:
    """
    Reads `path`, looks for a line starting with 'REVISION='
    and returns the string after '=' with whitespace stripped.
    Returns None if not found.
    """

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(prefix):
                return line[len(prefix):]
    return None

def get_info(directory: str) -> dict[str,str] | None:

    path = os.path.join(directory, "build.log")
    
    revision = get_one_info(path, "+ REVISION=")
    mysql = get_one_info(path, "-- MySQL")

    data = {
      "version": mysql,
      "revision": revision
    }
    return data

def fetch_params(base: str, user: str, token: str, job: str, build: str) -> dict[str, str]:
    url = f"{base.rstrip('/')}/job/{quote(job, safe='')}/{build}/api/json"

    r = httpx.get(
        url,
        auth=(user, token),
        params={"tree": "actions[parameters[name,value]]"},
        timeout=30,
    )
    r.raise_for_status()
    params: dict[str, str] = {}
    for action in r.json().get("actions", []):
        for p in action.get("parameters", []) or []:
            params[p["name"]] = "" if p.get("value") is None else str(p["value"])
    return params

def add_param(name: str, params: Dict[str, Any], data: Dict[str, Any]) -> None:
    if name in params:
        data[name.lower()] = params[name]

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url",    default=JENKINS_URL, help=f"Jenkins base URL (default: {JENKINS_URL})")
    ap.add_argument("--in_dir", default=".",         help=f"Input directory -with the build.log (default: current)")
    ap.add_argument("--out_dir",default=".",         help=f"Output directory (default: current)")
    ap.add_argument("--job",    default=JOB_NAME,    help=f"Job name (default: {JOB_NAME})")
    ap.add_argument("--build",                       help=f"Build number")
    ap.add_argument("--user",   default=os.environ.get("JENKINS_USER"),  help="Jenkins username (or env JENKINS_USER)")
    ap.add_argument("--token",  default=os.environ.get("JENKINS_TOKEN"), help="Jenkins API token (or env JENKINS_TOKEN)")
    args = ap.parse_args()

    if not args.user or not args.token:
        sys.exit("error: credentials missing. Pass --user/--token or set JENKINS_USER / JENKINS_TOKEN.")

    data = get_info(args.in_dir)

    try:
        params = fetch_params(args.url, args.user, args.token, args.job, args.build)
    except httpx.HTTPStatusError as e:
        sys.exit(f"HTTP {e.response.status_code} from {e.request.url}: {e.response.text[:200]}")

    add_param("BRANCH", params, data)
    add_param("PIPELINE_NAME", params, data)
    add_param("ARCH", params, data)
    add_param("GIT_REPO", params, data)
    add_param("DOCKER_OS", params, data)
    full_path = os.path.join(args.out_dir, args.build + ".json")
    #file_name = extract_from_line(path, "/binary.tar.gz")

    with open(full_path, "w", encoding="utf-8") as f:
      json.dump(data, f, indent=2, ensure_ascii=False)


    return 0


if __name__ == "__main__":
    raise SystemExit(main())