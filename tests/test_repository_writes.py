"""The suite stops any test that writes into the repository (see conftest.py)."""

import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import REPO, RepositoryWrite, pytest_configure, take_writes


def test_writes_into_the_repository_are_stopped_and_fail_the_test(request):
    # Neither path exists, so a broken guard fails these calls instead of changing the checkout.
    target = REPO / "tests" / f"leak-{uuid.uuid4().hex}"
    source = target.with_suffix(".source")
    attempts = [
        lambda: target.write_text("x", encoding="utf-8"),
        lambda: os.close(os.open(target, os.O_CREAT | os.O_WRONLY)),
        lambda: sqlite3.connect(target),
        lambda: sqlite3.connect(target.as_uri() + "?mode=rwc", uri=True),
        lambda: target.mkdir(),
        lambda: source.replace(target),
        lambda: target.unlink(),
        lambda: shutil.rmtree(target),
    ]
    for attempt in attempts:
        with pytest.raises(RepositoryWrite):
            attempt()
    assert not target.exists()
    writes = take_writes(request.node.nodeid)  # Taken, so this test is not failed for them.
    assert len(writes) == len(attempts)
    paths = {line.split(" ", 1)[1] for line in writes}
    assert paths == {os.path.normcase(source), os.path.normcase(target)}


def test_reads_and_temporary_files_are_left_alone(tmp_path, request):
    assert (REPO / "README.md").read_text(encoding="utf-8")
    (REPO / "pi").mkdir(exist_ok=True)  # Already there: nothing changes.
    with sqlite3.connect(tmp_path / "pi.db") as conn:
        conn.execute("CREATE TABLE t (x)")
    with sqlite3.connect((tmp_path / "pi.db").as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (0,)
    sqlite3.connect(":memory:").close()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert take_writes(request.node.nodeid) == []


@pytest.mark.parametrize("option", [True, False], ids=["basetemp", "temp-root"])
def test_a_temporary_directory_inside_the_repository_is_refused(monkeypatch, tmp_path, option):
    inside = str(REPO / ".test-tmp" / "full")
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path) if option else inside)
    config = SimpleNamespace(option=SimpleNamespace(basetemp=inside if option else None))
    with pytest.raises(pytest.UsageError, match="is inside the repository"):
        pytest_configure(config)
    config.option.basetemp = str(tmp_path)
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path))
    pytest_configure(config)  # Outside the checkout: accepted.


LEAKS = """
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("n", range(4))
def test_writes_a_file(n):
    (REPO / f"leak-{n}.txt").write_text("x", encoding="utf-8")


def test_hides_the_error():
    try:
        (REPO / "hidden").mkdir()
    except Exception:
        pass


def test_runs_in_a_worker(tmp_path):
    assert os.environ["PYTEST_XDIST_WORKER"].startswith("gw")
    (tmp_path / "fine.txt").write_text("x", encoding="utf-8")
"""


def test_every_xdist_worker_stops_and_reports_its_own_writes(tmp_path):
    repo = tmp_path / "repo"  # a checkout of its own, guarded by a copy of this conftest.py
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("conftest.py"), repo / "tests" / "conftest.py")
    (repo / "tests" / "test_leaks.py").write_text(textwrap.dedent(LEAKS), encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    command = [sys.executable, "-m", "pytest", "-n", "2", "-p", "no:cacheprovider", "-rf"]
    result = subprocess.run(
        [*command, f"--basetemp={tmp_path / 'run'}", "tests"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "5 failed, 1 passed" in output, output
    assert output.count("Tests may not write into the repository") >= 4, output
    assert "Wrote into the repository; keep test files in tmp_path" in output, output
    assert os.path.normcase(str(repo / "hidden")) in output, output
    assert sorted(path.name for path in repo.iterdir()) == ["pytest.ini", "tests"]
