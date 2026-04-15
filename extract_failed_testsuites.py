

#!/usr/bin/env python3
"""Create an XML file that contains only failed testcases from JUnit XML."""

from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import sys
import xml.etree.ElementTree as ET


def is_failed_testcase(testcase: ET.Element) -> bool:
    """Return True when a testcase has failure/error children."""
    return testcase.find("failure") is not None or testcase.find("error") is not None


def extract_failed_suites(root: ET.Element) -> ET.Element:
    """
    Build a new <testsuites> root containing only failed testcases.

    A testsuite is included only if it has at least one failed testcase.
    """
    output_root = ET.Element("testsuites")

    for testsuite in root.findall(".//testsuite"):
        failed_testcases = [
            copy.deepcopy(testcase)
            for testcase in testsuite.findall("./testcase")
            if is_failed_testcase(testcase)
        ]
        if not failed_testcases:
            continue

        suite_copy = ET.Element("testsuite", dict(testsuite.attrib))
        suite_copy.extend(failed_testcases)
        suite_copy.set("tests", str(len(failed_testcases)))
        suite_copy.set("failures", str(len(failed_testcases)))
        suite_copy.set("errors", "0")
        suite_copy.set("skip", "0")
        output_root.append(suite_copy)

    return output_root


def extract_failure_log(testcase: ET.Element) -> str:
    """Extract the most useful failure log text from a failed testcase."""
    system_out = testcase.find("system-out")
    if system_out is not None and (system_out.text or "").strip():
        return (system_out.text or "").strip()

    failure = testcase.find("failure")
    if failure is not None:
        message = failure.attrib.get("message", "").strip()
        text = (failure.text or "").strip()
        return text or message

    error = testcase.find("error")
    if error is not None:
        message = error.attrib.get("message", "").strip()
        text = (error.text or "").strip()
        return text or message

    return ""


def build_failed_test_records(
    root: ET.Element, source_xml_file: str
) -> list[dict[str, object]]:
    """Build flat JSON records for failed testcases from one source XML."""
    records: list[dict[str, object]] = []
    source_name = os.path.basename(source_xml_file)

    for testsuite in root.findall(".//testsuite"):
        suite_name = testsuite.attrib.get("name", "")
        package = testsuite.attrib.get("package", "")
        timestamp = testsuite.attrib.get("timestamp", "")

        for testcase in testsuite.findall("./testcase"):
            if not is_failed_testcase(testcase):
                continue

            test_name = testcase.attrib.get("name", "")
            full_name = f"{suite_name}.{test_name}" if suite_name else test_name

            records.append(
                {
                    "test_name": test_name,
                    "full_name": full_name,
                    "package": package,
                    "timestamp": timestamp,
                    "failure_log": extract_failure_log(testcase),
                    "source_xml_file": source_name,
                }
            )

    return records


def build_failed_tests_json(
    records: list[dict[str, object]],
) -> dict[str, object]:
    """Build final JSON output and apply aggregate failed_count."""
    total_failed = len(records)
    for record in records:
        record["failed_count"] = total_failed

    return {"failed_tests": records}


def resolve_input_files(input_paths: list[str]) -> list[str]:
    """
    Resolve input paths into a de-duplicated ordered list of XML files.

    - file path: includes file directly
    - directory: includes junit_WORKER*.xml inside that directory
    - glob pattern: expands pattern
    """
    resolved: list[str] = []
    seen: set[str] = set()

    for path in input_paths:
        matches: list[str] = []

        if os.path.isfile(path):
            matches = [path]
        elif os.path.isdir(path):
            matches = sorted(glob.glob(os.path.join(path, "junit_WORKER*.xml")))
        else:
            matches = sorted(glob.glob(path))

        for match in matches:
            absolute = os.path.abspath(match)
            if absolute in seen:
                continue
            seen.add(absolute)
            resolved.append(absolute)

    return resolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read one or more JUnit XML files and write output containing only "
            "failed testcases."
        )
    )
    parser.add_argument(
        "input_paths",
        nargs="+",
        help=(
            "Input file(s), glob pattern(s), or directory path(s). "
            "For a directory, junit_WORKER*.xml files are used."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default="failed_only.xml",
        help='Output file path (default: "failed_only.xml")',
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON output instead of XML",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_files = resolve_input_files(args.input_paths)
    if not input_files:
        print("No input XML files found.", file=sys.stderr)
        return 1

    combined_failed_root = ET.Element("testsuites")
    all_records: list[dict[str, object]] = []

    for input_file in input_files:
        root = ET.parse(input_file).getroot()
        failed_root = extract_failed_suites(root)
        source_name = os.path.basename(input_file)

        for testsuite in list(failed_root):
            testsuite.set("source_xml_file", source_name)
            combined_failed_root.append(testsuite)

        all_records.extend(build_failed_test_records(root, input_file))

    if args.json:
        output_data = build_failed_tests_json(all_records)
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(output_data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    else:
        tree = ET.ElementTree(combined_failed_root)
        ET.indent(tree, space="  ")
        tree.write(args.output, encoding="utf-8", xml_declaration=True)

    print(
        f"Wrote {len(all_records)} failed tests from "
        f"{len(input_files)} file(s) to {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())