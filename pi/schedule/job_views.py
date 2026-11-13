"""What the browser sees of a cron job and its runs: summaries, never raw arguments or receipts."""

from __future__ import annotations

from pi.schedule.job_models import (
    AgentTargetSummary,
    Definition,
    DefinitionSummary,
    JobView,
    RunActionView,
    RunView,
    TargetSummary,
    WatchSummary,
)


def browser_job(row):
    definition = Definition.model_validate(row["definition"])
    target = definition.target
    return JobView(
        id=row["id"],
        revision=row["revision"],
        definition=DefinitionSummary(
            name=definition.name,
            instructionsConfigured=bool(definition.instructions),
            instructions=definition.instructions,
            agentId=definition.agentId,
            agentName=row.get("agent_name") or None,
            timing=definition.timing,
            timeZone=definition.timeZone,
            enabled=definition.enabled,
            state="enabled" if definition.enabled else "paused",
            target=AgentTargetSummary(
                kind="agent",
                sessionId=row.get("agent_session_id"),
                webhook=bool(row.get("has_webhook")),
                clock=target.clock,
            )
            if target.kind == "agent"
            else TargetSummary(
                kind=target.kind,
                id=target.id,
                publishedVersion=target.publishedVersion,
                digest=target.digest,
                inputsConfigured=bool(target.args),
            ),
            watch=WatchSummary(
                toolId=definition.watch.toolId,
                everyMinutes=definition.watch.everyMinutes,
                inputsConfigured=bool(definition.watch.args),
                lastCheckedAt=row.get("watch_checked_at"),
                problem=row.get("watch_failure"),
            )
            if definition.watch
            else None,
            overlap=definition.overlap,
            requireBudget=definition.requireBudget,
            budgetAllowanceConfigured=definition.budgetAllowanceId is not None,
            createdBy=definition.createdBy,
            turnedOff=definition.turnedOff,
            adoptedAt=definition.adoptedAt,
        ),
        createdAt=row["created_at"],
        nextAt=row["next_at"],
    )


def browser_run(row):
    receipt = row.get("receipt")
    code = receipt.get("code") if isinstance(receipt, dict) else None
    if (
        not isinstance(code, str)
        or not code.isascii()
        or code != code.upper()
        or not code.replace("_", "").isalnum()
        or len(code) > 64
    ):
        code = None
    status = row["status"]
    execution = (
        "admitted-only"
        if status == "ready"
        else "waiting"
        if status in {"awaiting_budget", "awaiting_approval"}
        else "dispatched"
        if status == "dispatching"
        else "uncertain"
        if status == "outcome_unknown"
        else "resolved"
    )
    return RunView(
        id=row["id"],
        jobId=row["job_id"],
        jobRevision=row["job_revision"],
        scheduledAt=row["scheduled_at"],
        startedAt=row["started_at"],
        status=status,
        manual=row["request_id"] is not None,
        budgetBound=row.get("spending_budget_id") is not None,
        receiptRecorded=receipt is not None,
        outcomeCode=code,
        execution=execution,
    )


def browser_run_action(row, replayed):
    return RunActionView(**browser_run(row).model_dump(), replayed=replayed)
