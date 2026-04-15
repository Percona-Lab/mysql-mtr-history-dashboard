# mysql-mtr-history-dashboard
A dashboard to display the failing MTR tests with necessary details.

Scripts
1. Extract Jenkins Parameters, Build Metadata, and Failed Tests
Script: extarct_parametrs.py

What this script does:

- Fetches Jenkins build parameters from the build API JSON.
- Builds URLs from the build number.
- Downloads artifact archive.zip and stores it locally as BUILD_NUMBER.zip in the current directory.
- Reuses the local BUILD_NUMBER.zip on future runs if it already exists.
- Extracts build.log or build.log.gz from the archive.
Extracts:
   Revision from lines containing Revision=VALUE or REVISION=VALUE
   MySQL_version from lines containing -- MySQL VALUE
- Runs extract_failed_testsuites.py on junit_WORKER*.xml files found in the extracted archive.
- Merges failed_tests JSON output into the final JSON output.
Usage examples:
python3 extarct_parametrs.py --build-number 1559
python3 extarct_parametrs.py --build-number 1559 --username YOUR_USER --token YOUR_TOKEN
python3 extarct_parametrs.py --build-number 1559 -o params.json
python3 extarct_parametrs.py --build-number 1559 --job-base-url https://ps80.cd.percona.com/job/percona-server-8.0-pipeline-parallel-mtr -o params.json

2. Extract Only Failed Testcases from JUnit XML
Script: extract_failed_testsuites.py

What this script does:

- Reads one or more JUnit XML files.
- Keeps only failed or error testcases.
- Outputs XML by default.
- Outputs JSON when using --json.

Usage examples:
python3 extract_failed_testsuites.py "junit_WORKER*.xml" --json -o archive/work/results/failed_testsuites_all.json
python3 extract_failed_testsuites.py /path/to/results_dir --json -o failed_testsuites_all.json
python3 extract_failed_testsuites.py junit_WORKER1.xml junit_WORKER2.xml -o failed_only.xml
