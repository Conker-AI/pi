from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from . import (
    setup_choices,
    setup_model_probes,
    setup_models,
    setup_protection,
    setup_receipts,
    setup_rehearsal,
    setup_status,
)


def router(store, owner_key_configured, memory, toolgate, model_router, speech, authorize):
    routes = APIRouter(prefix="/setup", dependencies=[Depends(authorize)])

    @routes.get("/status", response_model=setup_status.SetupStatus)
    def status():
        return setup_status.load(
            store(),
            owner_key_configured=owner_key_configured(),
            memory=memory(),
            toolgate=toolgate(),
            router=model_router(),
            speech=speech(),
        )

    @routes.get("/receipts/{step}", response_model=setup_receipts.Receipt)
    def receipt(
        step: Annotated[setup_receipts.ReceiptStep, Path()],
    ):
        saved = setup_receipts.current(store(), step)
        if saved is None:
            raise HTTPException(404, "No setup evidence receipt has been recorded for this step.")
        return saved

    @routes.get("/choices/{step}", response_model=setup_choices.Choice)
    def choice(step: Annotated[setup_choices.ChoiceStep, Path()]):
        return setup_choices.current(store(), step)

    @routes.post("/choices/{step}", response_model=setup_choices.Choice)
    def record_choice(
        step: Annotated[setup_choices.ChoiceStep, Path()], body: setup_choices.ChoiceInput
    ):
        try:
            return setup_choices.record(store(), step, body)
        except setup_choices.ChoiceError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.detail}) from exc

    @routes.get("/boundaries")
    def boundaries():
        client = toolgate()
        if client is None:
            raise HTTPException(503, "ToolGate is not configured.")
        try:
            return client.policy_summary()
        except Exception as exc:
            raise HTTPException(503, "ToolGate policy is unavailable.") from exc

    @routes.get("/protection", response_model=setup_protection.Policy)
    def protection_policy():
        return setup_protection.current(store())

    @routes.post("/protection", response_model=setup_protection.Policy)
    def save_protection_policy(body: setup_protection.PolicyInput):
        try:
            return setup_protection.save(store(), body)
        except setup_protection.ProtectionError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.detail}) from exc

    @routes.get("/models", response_model=setup_models.Options)
    def model_options():
        return setup_models.options(store(), model_router())

    @routes.post("/models")
    def select_model(body: setup_models.Selection):
        try:
            return setup_models.select(store(), model_router(), body)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)) from exc

    @routes.post("/models/probe", response_model=setup_model_probes.ProbeReceipt)
    def probe_model(body: setup_model_probes.ProbeInput):
        try:
            return setup_model_probes.probe(store(), model_router(), body)
        except setup_model_probes.ProbeError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.detail}) from exc

    @routes.get("/rehearsal", response_model=setup_rehearsal.Status)
    def rehearsal_status():
        return setup_rehearsal.status(store())

    @routes.post("/rehearsal/memory-review", response_model=setup_rehearsal.Status)
    def rehearsal_memory_review(body: setup_rehearsal.MemoryReviewInput):
        try:
            return setup_rehearsal.review_memory(store(), body, memory())
        except setup_rehearsal.RehearsalError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.detail}) from exc

    @routes.post("/rehearsal/approval/start", response_model=setup_rehearsal.Status)
    def rehearsal_approval_start(body: setup_rehearsal.ApprovalInput):
        try:
            return setup_rehearsal.start_approval(store(), body, toolgate())
        except setup_rehearsal.RehearsalError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.detail}) from exc

    @routes.post("/rehearsal/approval/resume", response_model=setup_rehearsal.Status)
    def rehearsal_approval_resume(body: setup_rehearsal.ApprovalInput):
        try:
            return setup_rehearsal.resume_approval(store(), body, toolgate())
        except setup_rehearsal.RehearsalError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.detail}) from exc

    @routes.post("/rehearsal/finalize", response_model=setup_receipts.Receipt)
    def rehearsal_finalize():
        try:
            return setup_rehearsal.finalize(store())
        except setup_rehearsal.RehearsalError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.detail}) from exc

    @routes.post("/models/activate", response_model=setup_models.Activated)
    def activate_model(body: setup_models.Activation):
        try:
            return setup_models.activate(store(), model_router(), body)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)) from exc
        except setup_model_probes.ProbeError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.detail}) from exc

    @routes.post("/receipts/{step}", response_model=setup_receipts.Receipt)
    def record_receipt(
        step: Annotated[setup_receipts.ReceiptStep, Path()], body: setup_receipts.ReceiptInput
    ):
        try:
            if step == "rehearsal":
                raise setup_receipts.ReceiptError(
                    "server_evidence_required",
                    "Rehearsal evidence is created only by the verified first-run workflow.",
                    403,
                )
            if step == "protection":
                policy = setup_protection.current(store())
                expected_subject = setup_protection.receipt_subject(policy)
                if expected_subject is None:
                    raise setup_receipts.ReceiptError(
                        "protection_policy_missing",
                        "Configure an off-machine backup policy before recording evidence.",
                        409,
                    )
                if body.source != "conker.coordinated-backup" or body.subject != expected_subject:
                    raise setup_receipts.ReceiptError(
                        "protection_policy_changed",
                        "Protection evidence does not match the current backup policy.",
                        409,
                    )
            if step == "boundaries":
                client = toolgate()
                if client is None:
                    raise setup_receipts.ReceiptError(
                        "policy_unavailable", "ToolGate policy is unavailable.", 503
                    )
                try:
                    digest = client.policy_summary()["digest"]
                except Exception as exc:
                    raise setup_receipts.ReceiptError(
                        "policy_unavailable", "ToolGate policy is unavailable.", 503
                    ) from exc
                if body.evidenceDigest != digest:
                    raise setup_receipts.ReceiptError(
                        "policy_changed",
                        "ToolGate policy changed before the review was recorded.",
                        409,
                    )
            return setup_receipts.record(store(), step, body)
        except setup_receipts.ReceiptError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.detail}) from exc

    return routes
