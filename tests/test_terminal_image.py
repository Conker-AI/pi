from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_terminal_image_is_dedicated_pinned_and_non_root():
    dockerfile = (ROOT / "Dockerfile.terminal").read_text(encoding="utf-8")

    assert dockerfile.startswith(
        "FROM python:3.12.14-slim-trixie@sha256:"
        "f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f\n"
    )
    assert "COPY requirements.txt" not in dockerfile
    assert "COPY pi ./pi" not in dockerfile
    assert "COPY gateway" not in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/conker-terminal"]' in dockerfile
    assert "COPY --chmod=0444 pi/__init__.py /app/pi/__init__.py" in dockerfile
    assert "COPY --chmod=0444 pi/owner_terminal.py /app/pi/owner_terminal.py" in dockerfile
    assert "COPY --chmod=0444 pi/terminal_sidecar.py /app/pi/terminal_sidecar.py" in dockerfile


def test_terminal_launcher_is_fixed_and_does_not_load_site_packages():
    launcher = (ROOT / "scripts" / "conker-terminal").read_text(encoding="utf-8")

    assert launcher == (
        "#!/bin/sh\nset -eu\n\n"
        'exec python -I -S -c \'import sys; sys.path.insert(0,"/app"); '
        'from pi.terminal_sidecar import main; raise SystemExit(main())\' "$@"\n'
    )


def test_terminal_container_acceptance_covers_hostile_replacement_and_boundaries():
    acceptance = (ROOT / "scripts" / "terminal_container_acceptance.sh").read_text(encoding="utf-8")
    probe = (ROOT / "scripts" / "terminal_container_probe.py").read_text(encoding="utf-8")

    for required in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 64",
        "--user 65532:65532",
        "--gateway-uid 1000",
        "fake-listener-accepted",
    ):
        assert required in acceptance
    assert 'choices=("normal", "attack")' in probe
    assert "fake-listener-ready" in probe
    assert "os.kill(pid,signal.SIGTERM)" in probe
    assert "supervisor termination did not close the gateway channel" in probe
    assert "GATEWAY_" in probe and "/var/run/docker.sock" in probe
