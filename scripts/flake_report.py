"""Summarise pytest JUnit XML as markdown: totals, reruns, failures, skips, slowest tests.

    python scripts/flake_report.py junit.xml [more.xml ...] [--top N]

Prints the report and, when $GITHUB_STEP_SUMMARY is set, appends it there too (the CI job summary).
CI keeps the XML as an artifact. Nothing here retries a test or hides a failure: a test that ran
more than once (a rerun plugin, or two reports merged) is listed under Reruns with every attempt,
so a flake stays visible. A missing or unreadable file is reported, not raised: the report step
must never be the reason a job fails. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

Case = tuple[str, float, str, str]  # (test id, seconds, outcome, detail)


def outcome(case: ET.Element) -> tuple[str, str]:
    for tag, label in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
        child = case.find(tag)
        if child is not None:
            first_line = (child.text or "").strip().split("\n")[0]
            return label, child.get("message") or first_line
    return "passed", ""


def read_cases(paths: list[Path]) -> tuple[list[Case], list[str]]:
    cases: list[Case] = []
    problems: list[str] = []
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as error:
            problems.append(f"{path}: {error}")
            continue
        for case in root.iter("testcase"):
            name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
            label, detail = outcome(case)
            cases.append(
                (name, float(case.get("time") or 0), label, detail.replace("\n", " ")[:160])
            )
    return cases, problems


def cell(text: str) -> str:
    return text.replace("|", "\\|")


def report(cases: list[Case], problems: list[str], top: int) -> str:
    lines = ["## Test report", ""]
    for problem in problems:
        lines.append(f"- could not read {cell(problem)}")
    if not cases:
        lines.append("- no test results found")
        return "\n".join(lines) + "\n"
    counts = defaultdict(int)
    for _, _, label, _ in cases:
        counts[label] += 1
    total = sum(seconds for _, seconds, _, _ in cases)
    lines.append(
        f"{len(cases)} results: {counts['passed']} passed, {counts['failed']} failed, "
        f"{counts['error']} errors, {counts['skipped']} skipped; {total:.0f} s of test time."
    )
    attempts: dict[str, list[str]] = defaultdict(list)
    for name, _, label, _ in cases:
        attempts[name].append(label)
    reruns = {name: seen for name, seen in attempts.items() if len(seen) > 1}
    lines += ["", f"### Reruns ({len(reruns)})", ""]
    if reruns:
        lines += ["| test | attempts, in order |", "| --- | --- |"]
        lines += [
            f"| `{cell(name)}` | {' -> '.join(seen)} |" for name, seen in sorted(reruns.items())
        ]
    else:
        lines.append("None: every test ran once.")
    failed = [case for case in cases if case[2] in ("failed", "error")]
    if failed:
        lines += ["", f"### Failures ({len(failed)})", ""]
        lines += [
            f"- `{cell(name)}` ({label}): {cell(detail)}" for name, _, label, detail in failed
        ]
    skipped = [case for case in cases if case[2] == "skipped"]
    lines += ["", f"### Skips ({len(skipped)})", ""]
    if skipped:
        by_reason: dict[str, list[str]] = defaultdict(list)
        for name, _, _, detail in skipped:
            by_reason[detail or "no reason given"].append(name)
        for reason, names in sorted(by_reason.items(), key=lambda item: -len(item[1])):
            lines.append(
                f"- {len(names)} x {cell(reason)}: `{cell(names[0])}`"
                + (" ..." if len(names) > 1 else "")
            )
    else:
        lines.append("None.")
    slowest = sorted(cases, key=lambda case: -case[1])[:top]
    lines += ["", f"### Slowest {len(slowest)}", "", "| seconds | test |", "| ---: | --- |"]
    lines += [f"| {seconds:.2f} | `{cell(name)}` |" for name, seconds, _, _ in slowest]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Summarise pytest JUnit XML as markdown.")
    parser.add_argument("junit", nargs="+", type=Path, help="pytest --junitxml file(s)")
    parser.add_argument("--top", type=int, default=10, help="how many slow tests to list")
    args = parser.parse_args(argv)
    cases, problems = read_cases(args.junit)
    text = report(cases, problems, args.top)
    sys.stdout.write(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
