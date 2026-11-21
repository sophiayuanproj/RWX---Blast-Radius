"""pytest plugin: write the nodeids of failing tests (any failed report,
subtests included) and of files that failed to collect to $LABEL_OUT."""

import json
import os

failed = set()
bad_files = set()


def pytest_runtest_logreport(report):
    if report.failed:
        failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        bad_files.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["LABEL_OUT"], "w") as f:
        json.dump({"failed": sorted(failed), "bad_files": sorted(bad_files)}, f)
