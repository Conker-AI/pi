#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: terminal_container_acceptance.sh IMAGE" >&2
    exit 2
fi

image=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
probe=$script_dir/terminal_container_probe.py
python_image=python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
run_id=conker-terminal-acceptance-$$
workspace=$(mktemp -d)
scratch=$(mktemp -d)
containers=""
volumes=""

cleanup() {
    for container in $containers; do
        docker rm -f "$container" >/dev/null 2>&1 || true
    done
    for volume in $volumes; do
        docker volume rm "$volume" >/dev/null 2>&1 || true
    done
    rm -rf "$scratch"
    docker run --rm -v /tmp:/host "$python_image" \
        rm -rf "/host/$(basename "$workspace")" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker image inspect "$image" >/dev/null
docker run --rm --user 0 --entrypoint sh -v "$workspace:/workspace" "$image" \
    -c 'chown 65532:65532 /workspace && chmod 0770 /workspace'

inspect_boundary() {
    container=$1
    inspection=$scratch/$container.json
    docker inspect "$container" > "$inspection"
    python3 - "$inspection" "$workspace" <<'PY'
import json
import pathlib
import platform
import sys

container = json.loads(pathlib.Path(sys.argv[1]).read_text())[0]
host = container["HostConfig"]
config = container["Config"]
mounts = [item for item in container["Mounts"] if item["Type"] in {"bind", "volume"}]
assert host["NetworkMode"] == "none"
assert host["ReadonlyRootfs"] is True
assert host["CapDrop"] == ["ALL"]
assert host["SecurityOpt"] == ["no-new-privileges"]
assert host["PidsLimit"] == 64
assert config["User"] == "65532:65532"
assert config["Entrypoint"] == ["/usr/local/bin/conker-terminal"]
assert len(mounts) == 2
assert {item["Destination"] for item in mounts} == {
    "/run/conker-terminal", "/workspace"
}
workspace = next(item for item in mounts if item["Destination"] == "/workspace")
expected_workspace = str(pathlib.Path(sys.argv[2]).resolve())
if workspace["Source"] != expected_workspace:
    assert "microsoft" in platform.release().lower(), (
        f"workspace source changed: {workspace['Source']!r} != {expected_workspace!r}"
    )
for entry in config["Env"]:
    name = entry.split("=", 1)[0]
    assert not name.startswith(
        ("PI_", "GATEWAY_", "TOOLGATE_", "MEMORYGATE_", "OPENAI_", "ANTHROPIC_", "OPENROUTER_")
    )
PY
}

run_case() {
    mode=$1
    control=${run_id}-${mode}
    sidecar=${run_id}-${mode}-sidecar
    gateway=${run_id}-${mode}-gateway
    volumes="$volumes $control"
    containers="$containers $sidecar"
    docker volume create "$control" >/dev/null
    docker run -d --name "$sidecar" \
        --network none --read-only --init --cap-drop ALL \
        --security-opt no-new-privileges --pids-limit 64 --memory 256m --cpus 1 \
        --stop-timeout 10 --user 65532:65532 \
        --env CONKER_TERMINAL_ISOLATED=1 \
        --mount "type=volume,src=$control,dst=/run/conker-terminal" \
        --mount "type=bind,src=$workspace,dst=/workspace" \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
        --health-cmd 'conker-terminal health --health /run/conker-terminal/health.json' \
        --health-interval 1s --health-timeout 2s --health-retries 10 \
        "$image" serve \
        --socket /run/conker-terminal/control.sock \
        --health /run/conker-terminal/health.json \
        --shell /bin/bash --workspace /workspace --gateway-uid 1000 >/dev/null
    inspect_boundary "$sidecar"

    deadline=$(( $(date +%s) + 20 ))
    while [ "$(docker inspect -f '{{.State.Health.Status}}' "$sidecar")" != healthy ]; do
        [ "$(date +%s)" -lt "$deadline" ] || {
            docker logs "$sidecar" >&2
            echo "owner-terminal did not become healthy" >&2
            exit 1
        }
        sleep 1
    done

    docker run --rm --name "$gateway" --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges --pids-limit 32 \
        --memory 128m --user 1000:65532 \
        --mount "type=volume,src=$control,dst=/run/conker-terminal" \
        --mount "type=bind,src=$probe,dst=/probe.py,readonly" \
        "$python_image" python /probe.py \
        /run/conker-terminal/control.sock --mode "$mode"

    exit_code=$(docker wait "$sidecar")
    [ "$exit_code" -eq 0 ] || {
        docker logs "$sidecar" >&2
        echo "owner-terminal exited $exit_code during $mode probe" >&2
        exit 1
    }
    docker rm "$sidecar" >/dev/null
    containers=$(printf '%s' "$containers" | sed "s/ $sidecar//")
}

run_case normal
run_case attack

docker run --rm -v "$workspace:/workspace:ro" "$python_image" python -c '
from pathlib import Path
root = Path("/workspace")
assert (root / "fake-listener-ready").read_text(encoding="ascii") == "ready"
assert not (root / "fake-listener-accepted").exists()
'

image_id=$(docker image inspect "$image" --format '{{.Id}}')
printf '{"status":"passed","image":%s,"imageId":%s,"normal":true,"hostileListener":true,"supervisorLoss":true}\n' \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$image")" \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$image_id")"
