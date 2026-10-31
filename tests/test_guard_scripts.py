"""The CI guards: file sizes stay under 600 lines and cross-package imports stay listed.

Nothing else exercises scripts/check_file_sizes.py or scripts/check_package_boundaries.py,
so an edit there could quietly loosen what CI enforces. Each case builds a throwaway tree
in tmp_path — a git repo for the file-size check, which reads `git ls-files` — runs the
script from it, and pins the script's real message so a weaker rule cannot pass unnoticed.
"""

import subprocess
import sys
from pathlib import Path

FILE_SIZES = Path(__file__).resolve().parents[1] / "scripts" / "check_file_sizes.py"
PACKAGE_BOUNDARIES = Path(__file__).resolve().parents[1] / "scripts" / "check_package_boundaries.py"


def run_script(tmp_path, script):
    return subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, check=False
    )


def write(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def tracked(tmp_path, files):
    """Write `files` and stage them, so `git ls-files` in the script sees them as tracked."""
    write(tmp_path, files)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)


def file_allowlist(tmp_path, entries):
    write(tmp_path, {"file-size-allowlist.txt": "\n".join(entries) + "\n"})


def boundaries(tmp_path, edges):
    write(tmp_path, {"package-boundaries.txt": "\n".join(edges) + "\n"})


def test_file_at_the_limit_passes(tmp_path):
    tracked(tmp_path, {"big.py": "x\n" * 600})
    result = run_script(tmp_path, FILE_SIZES)
    assert result.returncode == 0
    assert result.stdout == "file sizes ok (limit 600, 0 allowlisted files)\n"


def test_file_over_the_limit_fails(tmp_path):
    tracked(tmp_path, {"big.py": "x\n" * 601})
    result = run_script(tmp_path, FILE_SIZES)
    assert result.returncode == 1
    assert result.stdout == "big.py: 601 lines, limit 600. Split it by concern.\n"


def test_allowlisted_file_at_its_ceiling_passes(tmp_path):
    tracked(tmp_path, {"big.py": "x\n" * 610})
    file_allowlist(tmp_path, ["big.py 610"])
    result = run_script(tmp_path, FILE_SIZES)
    assert result.returncode == 0
    assert result.stdout == "file sizes ok (limit 600, 1 allowlisted files)\n"


def test_allowlisted_file_over_its_ceiling_fails(tmp_path):
    tracked(tmp_path, {"big.py": "x\n" * 611})
    file_allowlist(tmp_path, ["big.py 610"])
    result = run_script(tmp_path, FILE_SIZES)
    assert result.returncode == 1
    assert (
        "big.py: grew to 611 lines; it is allowlisted at 610 and may only shrink." in result.stdout
    )


def test_allowlisted_file_under_the_limit_is_asked_to_drop_the_entry(tmp_path):
    tracked(tmp_path, {"big.py": "x\n" * 500})
    file_allowlist(tmp_path, ["big.py 610"])
    result = run_script(tmp_path, FILE_SIZES)
    assert result.returncode == 1
    assert (
        "big.py: now 500 lines, under the limit; delete its line from file-size-allowlist.txt."
        in result.stdout
    )


def test_allowlisted_file_between_limit_and_ceiling_is_asked_to_lower_the_entry(tmp_path):
    tracked(tmp_path, {"big.py": "x\n" * 605})
    file_allowlist(tmp_path, ["big.py 610"])
    result = run_script(tmp_path, FILE_SIZES)
    assert result.returncode == 1
    assert (
        "big.py: shrank to 605 lines; lower its entry in file-size-allowlist.txt from 610."
        in result.stdout
    )


def test_untracked_allowlist_entry_fails(tmp_path):
    tracked(tmp_path, {"kept.py": "x\n"})
    file_allowlist(tmp_path, ["gone.py 700"])
    result = run_script(tmp_path, FILE_SIZES)
    assert result.returncode == 1
    assert (
        "gone.py: listed in file-size-allowlist.txt but no longer tracked; delete the line."
        in result.stdout
    )


def test_loose_module_in_pi_fails(tmp_path):
    boundaries(tmp_path, [])
    write(tmp_path, {"pi/loose.py": "x = 1\n"})
    result = run_script(tmp_path, PACKAGE_BOUNDARIES)
    assert result.returncode == 1
    assert result.stdout == (
        "pi/loose.py: modules live in a package named for their type, not loose in pi/.\n"
    )


def test_unlisted_cross_package_import_fails_and_names_the_edge(tmp_path):
    boundaries(tmp_path, [])
    write(tmp_path, {"pi/a/worker.py": "import pi.b\n"})
    result = run_script(tmp_path, PACKAGE_BOUNDARIES)
    assert result.returncode == 1
    assert (
        "a -> b (pi/a/worker.py): a new cross-package import. Move the code to the package it "
        "belongs to, or add the edge to package-boundaries.txt and say why." in result.stdout
    )


def test_listed_edge_passes(tmp_path):
    boundaries(tmp_path, ["a -> b"])
    write(tmp_path, {"pi/a/worker.py": "import pi.b\n"})
    result = run_script(tmp_path, PACKAGE_BOUNDARIES)
    assert result.returncode == 0
    assert result.stdout == "package boundaries ok (1 cross-package edges, all listed)\n"


def test_listed_edge_no_longer_imported_fails(tmp_path):
    boundaries(tmp_path, ["a -> b"])
    write(tmp_path, {"pi/__init__.py": ""})
    result = run_script(tmp_path, PACKAGE_BOUNDARIES)
    assert result.returncode == 1
    assert result.stdout == (
        "a -> b: listed in package-boundaries.txt but no longer imported; delete the line.\n"
    )


def test_api_import_fails_even_when_listed(tmp_path):
    boundaries(tmp_path, ["memory -> api"])
    write(tmp_path, {"pi/memory/export.py": "import pi.api\n"})
    result = run_script(tmp_path, PACKAGE_BOUNDARIES)
    assert result.returncode == 1
    assert result.stdout == (
        "memory -> api (pi/memory/export.py): only the HTTP layer may import pi.api.\n"
    )
