"""Host CLI client for durable Pi setup evidence receipts."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

import httpx
from pydantic import ValidationError

from pi.browser_contract import owner_allowed
from pi.setup_choices import Choice, ChoiceInput
from pi.setup_model_probes import ProbeInput, ProbeReceipt
from pi.setup_models import Options, Selection
from pi.setup_protection import Policy, PolicyInput
from pi.setup_receipts import Receipt, ReceiptInput
from pi.setup_rehearsal import Status as RehearsalStatus

from .api import Config

MAX_INPUT_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
DIGEST = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


class SetupControlError(ValueError):
    pass


def _owner_headers(config: Config) -> dict[str, str]:
    if not config.pi_owner_key:
        raise SetupControlError("Pi owner setup control is not configured.")
    return {
        "Accept": "application/json",
        "X-Pi-Owner-Key": config.pi_owner_key,
    }


def _request_json(
    config: Config,
    client: httpx.Client,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    missing: bool = False,
) -> dict | None:
    if not owner_allowed(method, path):
        raise SetupControlError("Setup operation is outside the owner allowlist.")
    headers = _owner_headers(config)
    try:
        with client.stream(
            method,
            config.pi_url.rstrip("/") + path,
            headers=headers,
            json=body,
        ) as response:
            if missing and response.status_code == 404:
                return None
            if response.status_code not in {200, 201}:
                raise SetupControlError(
                    f"Pi rejected the setup operation (HTTP {response.status_code})."
                )
            content_type = response.headers.get("content-type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise SetupControlError("Pi returned a non-JSON setup response.")
            declared = response.headers.get("content-length")
            if declared and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES):
                raise SetupControlError("Pi setup response exceeded 256 KiB.")
            value = bytearray()
            for chunk in response.iter_bytes():
                value.extend(chunk)
                if len(value) > MAX_RESPONSE_BYTES:
                    raise SetupControlError("Pi setup response exceeded 256 KiB.")
    except SetupControlError:
        raise
    except (httpx.HTTPError, OSError):
        raise SetupControlError("Pi setup control is unavailable.") from None
    try:
        decoded = json.loads(value.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SetupControlError("Pi returned invalid setup JSON.") from None
    if not isinstance(decoded, dict):
        raise SetupControlError("Pi returned an unsupported setup response.")
    return decoded


def _current_revision(config: Config, client: httpx.Client, step: str) -> int:
    current = _request_json(config, client, "GET", f"/setup/receipts/{step}", missing=True)
    if current is None:
        return 0
    try:
        receipt = Receipt.model_validate(current, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported current setup receipt.") from None
    if receipt.step != step:
        raise SetupControlError("Pi returned a mismatched current setup receipt.")
    return receipt.revision


def _record(config: Config, client: httpx.Client, step: str, payload: dict) -> dict:
    revision = _current_revision(config, client, step)
    try:
        value = ReceiptInput.model_validate({**payload, "expectedRevision": revision})
    except ValidationError:
        raise SetupControlError("Setup receipt has an unsupported shape.") from None
    saved = _request_json(
        config,
        client,
        "POST",
        f"/setup/receipts/{step}",
        body=value.model_dump(mode="json"),
    )
    try:
        receipt = Receipt.model_validate(saved, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported saved setup receipt.") from None
    if (
        receipt.step != step
        or receipt.receiptId != value.receiptId
        or receipt.evidenceDigest != value.evidenceDigest
        or receipt.revision <= revision
    ):
        raise SetupControlError("Pi returned a mismatched saved setup receipt.")
    return receipt.model_dump(mode="json")


def record_external_receipt(
    config: Config,
    step: str,
    stream: BinaryIO,
    client: httpx.Client,
) -> dict:
    if step != "protection":
        raise SetupControlError("Only protection accepts external evidence.")
    raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise SetupControlError("Setup receipt exceeds 16 KiB.")
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SetupControlError("Setup receipt is invalid JSON.") from None
    expected = {
        "receiptId",
        "source",
        "subject",
        "evidenceDigest",
        "completedAt",
        "expiresAt",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise SetupControlError("Setup receipt has an unsupported shape.")
    return _record(config, client, step, payload)


def review_boundaries(
    config: Config,
    expected_digest: str,
    client: httpx.Client,
    *,
    now: datetime | None = None,
) -> dict:
    if not DIGEST.fullmatch(expected_digest):
        raise SetupControlError("Provide the lowercase SHA-256 policy digest you reviewed.")
    policy = _request_json(config, client, "GET", "/setup/boundaries")
    if policy is None or policy.get("digest") != expected_digest:
        raise SetupControlError("ToolGate policy changed; inspect boundaries and review it again.")
    completed = (now or datetime.now(UTC)).astimezone(UTC)
    payload = {
        "receiptId": f"setup-boundaries-{uuid.uuid4()}",
        "source": "conker.cli",
        "subject": "toolgate.policy",
        "evidenceDigest": expected_digest,
        "completedAt": completed.isoformat(),
        "expiresAt": (completed + timedelta(days=30)).isoformat(),
    }
    return _record(config, client, "boundaries", payload)


def protection_policy(config: Config, client: httpx.Client) -> dict:
    value = _request_json(config, client, "GET", "/setup/protection")
    try:
        result = Policy.model_validate(value, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported protection policy.") from None
    return result.model_dump(mode="json")


def set_protection_policy(config: Config, stream: BinaryIO, client: httpx.Client) -> dict:
    raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise SetupControlError("Protection policy exceeds 16 KiB.")
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SetupControlError("Protection policy is invalid JSON.") from None
    if not isinstance(payload, dict) or set(payload) != {"destination", "retentionCopies"}:
        raise SetupControlError("Protection policy has an unsupported shape.")
    current = Policy.model_validate(protection_policy(config, client), strict=False)
    try:
        body = PolicyInput(
            requestId=f"setup-protection-{uuid.uuid4()}",
            destination=payload["destination"],
            retentionCopies=payload["retentionCopies"],
            expectedRevision=current.revision,
        )
    except ValidationError:
        raise SetupControlError("Protection policy has invalid values.") from None
    value = _request_json(
        config,
        client,
        "POST",
        "/setup/protection",
        body=body.model_dump(mode="json"),
    )
    try:
        saved = Policy.model_validate(value, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported saved protection policy.") from None
    if (
        saved.revision != current.revision + 1
        or saved.requestId != body.requestId
        or saved.destination != body.destination
        or saved.retentionCopies != body.retentionCopies
    ):
        raise SetupControlError("Pi returned a mismatched saved protection policy.")
    return saved.model_dump(mode="json")


def set_setup_choice(
    config: Config,
    step: str,
    choice: str,
    client: httpx.Client,
) -> dict:
    valid = (step == "companion" and choice == "accept") or (
        step in {"memory", "capabilities"} and choice in {"include", "skip"}
    )
    if not valid:
        raise SetupControlError(
            "Setup choices use companion:accept or memory|capabilities:include|skip."
        )
    path = f"/setup/choices/{step}"
    current = _request_json(config, client, "GET", path)
    try:
        observed = Choice.model_validate(current, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported setup choice.") from None
    if observed.step != step:
        raise SetupControlError("Pi returned a mismatched setup choice.")
    value = ChoiceInput(
        requestId=f"setup-choice-{step}-{uuid.uuid4()}",
        choice=choice,
        expectedRevision=observed.revision,
    )
    saved = _request_json(config, client, "POST", path, body=value.model_dump(mode="json"))
    try:
        result = Choice.model_validate(saved, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported saved setup choice.") from None
    if (
        result.step != step
        or result.choice != choice
        or result.requestId != value.requestId
        or result.revision != observed.revision + 1
    ):
        raise SetupControlError("Pi returned a mismatched saved setup choice.")
    return result.model_dump(mode="json")


def rehearsal_status(config: Config, client: httpx.Client) -> dict:
    value = _request_json(config, client, "GET", "/setup/rehearsal")
    return _parse_rehearsal(value)


def _parse_rehearsal(value: dict | None) -> dict:
    try:
        result = RehearsalStatus.model_validate(value, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported rehearsal status.") from None
    return result.model_dump(mode="json")


def review_rehearsal_memory(config: Config, client: httpx.Client) -> dict:
    current = _request_json(config, client, "GET", "/setup/choices/memory")
    try:
        choice = Choice.model_validate(current, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported memory choice.") from None
    if choice.choice not in {"include", "skip"}:
        raise SetupControlError("Choose whether to include or skip memory before reviewing it.")
    value = _request_json(
        config,
        client,
        "POST",
        "/setup/rehearsal/memory-review",
        body={
            "requestId": f"setup-memory-review-{uuid.uuid4()}",
            "expectedChoiceRevision": choice.revision,
        },
    )
    return _parse_rehearsal(value)


def start_rehearsal_approval(config: Config, client: httpx.Client) -> dict:
    request_id = f"setup-approval-{uuid.uuid4()}"
    value = _request_json(
        config,
        client,
        "POST",
        "/setup/rehearsal/approval/start",
        body={"requestId": request_id},
    )
    result = RehearsalStatus.model_validate(_parse_rehearsal(value), strict=False)
    if result.approvalRequestId != request_id:
        raise SetupControlError("Pi returned a mismatched rehearsal approval.")
    return result.model_dump(mode="json")


def resume_rehearsal_approval(config: Config, request_id: str, client: httpx.Client) -> dict:
    if not REQUEST_ID.fullmatch(request_id):
        raise SetupControlError("Use the approval request ID returned by the rehearsal status.")
    value = _request_json(
        config,
        client,
        "POST",
        "/setup/rehearsal/approval/resume",
        body={"requestId": request_id},
    )
    result = RehearsalStatus.model_validate(_parse_rehearsal(value), strict=False)
    if result.approvalRequestId != request_id:
        raise SetupControlError("Pi returned a mismatched rehearsal approval.")
    return result.model_dump(mode="json")


def finalize_rehearsal(config: Config, client: httpx.Client) -> dict:
    value = _request_json(config, client, "POST", "/setup/rehearsal/finalize", body={})
    try:
        receipt = Receipt.model_validate(value, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported rehearsal receipt.") from None
    if receipt.step != "rehearsal" or receipt.source != "conker.first-run-rehearsal":
        raise SetupControlError("Pi returned a mismatched rehearsal receipt.")
    return receipt.model_dump(mode="json")


def model_options(config: Config, client: httpx.Client) -> dict:
    value = _request_json(config, client, "GET", "/setup/models")
    try:
        result = Options.model_validate(value, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned unsupported setup model options.") from None
    return result.model_dump(mode="json")


def set_setup_model(config: Config, candidate_id: str, client: httpx.Client) -> dict:
    if not isinstance(candidate_id, str) or not candidate_id.strip() or len(candidate_id) > 200:
        raise SetupControlError("Choose a candidate ID returned by conker setup models.")
    current = Options.model_validate(model_options(config, client), strict=False)
    candidate = next((item for item in current.candidates if item.id == candidate_id), None)
    if candidate is None:
        raise SetupControlError("Choose a candidate ID returned by conker setup models.")
    if candidate.status == "unavailable":
        raise SetupControlError("That answer model is unavailable on the Conker host.")
    body = Selection(candidateId=candidate_id, expectedRevision=current.revision)
    saved = _request_json(
        config, client, "POST", "/setup/models", body=body.model_dump(mode="json")
    )
    if (
        not isinstance(saved, dict)
        or saved.get("revision") != current.revision + 1
        or not isinstance(saved.get("configuration"), dict)
        or saved["configuration"].get("defaultModelId") != candidate_id
    ):
        raise SetupControlError("Pi returned a mismatched saved answer model.")
    return saved


def probe_setup_model(
    config: Config,
    candidate_id: str,
    configuration_revision: int,
    client: httpx.Client,
) -> dict:
    value = ProbeInput(
        requestId=f"setup-model-probe-{uuid.uuid4()}",
        candidateId=candidate_id,
        expectedRevision=configuration_revision,
    )
    saved = _request_json(
        config,
        client,
        "POST",
        "/setup/models/probe",
        body=value.model_dump(mode="json"),
    )
    try:
        result = ProbeReceipt.model_validate(saved, strict=False)
    except ValidationError:
        raise SetupControlError("Pi returned an unsupported model probe receipt.") from None
    if (
        result.requestId != value.requestId
        or result.candidateId != candidate_id
        or result.configurationRevision != configuration_revision
    ):
        raise SetupControlError("Pi returned a mismatched model probe receipt.")
    return result.model_dump(mode="json")


def activate_setup_model(config: Config, candidate_id: str, client: httpx.Client) -> dict:
    if not isinstance(candidate_id, str) or not candidate_id.strip() or len(candidate_id) > 200:
        raise SetupControlError("Choose a candidate ID returned by conker setup models.")
    current = Options.model_validate(model_options(config, client), strict=False)
    candidate = next((item for item in current.candidates if item.id == candidate_id), None)
    if candidate is None:
        raise SetupControlError("Choose a candidate ID returned by conker setup models.")
    if candidate.status == "unavailable":
        raise SetupControlError("That answer model is unavailable on the Conker host.")
    request_id = f"setup-model-probe-{uuid.uuid4()}"
    saved = _request_json(
        config,
        client,
        "POST",
        "/setup/models/activate",
        body={
            "requestId": request_id,
            "candidateId": candidate_id,
            "expectedRevision": current.revision,
        },
    )
    try:
        probe = ProbeReceipt.model_validate(saved.get("probe"), strict=False)
    except (AttributeError, ValidationError):
        raise SetupControlError("Pi returned an unsupported activated model receipt.") from None
    if (
        saved.get("revision") != current.revision + 1
        or saved.get("candidateId") != candidate_id
        or probe.requestId != request_id
        or probe.configurationRevision != saved["revision"]
        or probe.candidateId != candidate_id
    ):
        raise SetupControlError("Pi returned a mismatched activated answer model.")
    return saved
