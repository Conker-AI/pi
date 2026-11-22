"""scripts/flake_report.py: what it lists from a pytest JUnit file, and that it never fails a job."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "flake_report.py"

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
<testcase classname="tests.test_a" name="test_fast" time="0.01"/>
<testcase classname="tests.test_a" name="test_slow" time="9.50"/>
<testcase classname="tests.test_a" name="test_flaky" time="0.20"><failure message="boom">x</failure></testcase>
<testcase classname="tests.test_a" name="test_flaky" time="0.30"/>
<testcase classname="tests.test_b" name="test_skipped" time="0">
<skipped type="pytest.skip" message="needs docker"/></testcase>
</testsuite></testsuites>
"""


def run(tmp_path, *names, summary=None):
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    if summary:
        env["GITHUB_STEP_SUMMARY"] = str(summary)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *[str(tmp_path / name) for name in names]],
        capture_output=True,
        text=True,
        env=env,
    )


def test_report_lists_reruns_skips_and_slowest(tmp_path):
    (tmp_path / "junit.xml").write_text(JUNIT, encoding="utf-8")
    done = run(tmp_path, "junit.xml")
    assert done.returncode == 0
    out = done.stdout
    assert "5 results: 3 passed, 1 failed, 0 errors, 1 skipped" in out
    assert "### Reruns (1)" in out
    assert "| `tests.test_a::test_flaky` | failed -> passed |" in out
    assert "- 1 x needs docker: `tests.test_b::test_skipped`" in out
    assert "### Failures (1)" in out
    assert out.index("| 9.50 | `tests.test_a::test_slow` |") < out.index("| 0.30 |")


def test_report_appends_to_the_job_summary(tmp_path):
    summary = tmp_path / "summary.md"
    summary.write_text("earlier\n", encoding="utf-8")
    (tmp_path / "junit.xml").write_text(JUNIT, encoding="utf-8")
    done = run(tmp_path, "junit.xml", summary=summary)
    assert summary.read_text(encoding="utf-8") == "earlier\n" + done.stdout


def test_missing_or_broken_files_are_reported_and_exit_zero(tmp_path):
    (tmp_path / "broken.xml").write_text("<testsuites>", encoding="utf-8")
    done = run(tmp_path, "missing.xml", "broken.xml")
    assert done.returncode == 0
    assert "could not read" in done.stdout
    assert "no test results found" in done.stdout
