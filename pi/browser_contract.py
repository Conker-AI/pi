"""The explicit conversation capabilities granted to the browser gateway."""

import re

RUNTIME_ROUTES = {
    "GET": (
        r"/health",
        r"/sessions",
        r"/sessions/[A-Za-z0-9_-]+",
        r"/sessions/[A-Za-z0-9_-]+/submissions",
        r"/turns/unreplied",
        r"/turn-submissions/[A-Za-z0-9_-]+",
        r"/turn-submissions/[A-Za-z0-9_-]+/stream",
        r"/approvals",
        r"/tools",
        r"/models",
        r"/messages/[A-Za-z0-9_-]+",
        r"/memory",
        r"/tasks",
        r"/tasks/[A-Za-z0-9_-]+",
        r"/tasks/requests/[A-Za-z0-9_-]+",
        r"/runs",
        r"/runs/[A-Za-z0-9_-]+",
        r"/events",
        r"/proposals",
        r"/proposals/passes",
    ),
    "POST": (
        r"/sessions",
        r"/sessions/[A-Za-z0-9_-]+/turns",
        r"/sessions/[A-Za-z0-9_-]+/fork",
        r"/turns/[A-Za-z0-9_-]+/resume",
        r"/turn-submissions/[A-Za-z0-9_-]+/cancel",
        r"/tasks",
        r"/tasks/[A-Za-z0-9_-]+/(update|transition|archive)",
        r"/proposals/[A-Za-z0-9_-]+/decision",
    ),
}


def runtime_allowed(method: str, path: str) -> bool:
    return any(re.fullmatch(pattern, path) for pattern in RUNTIME_ROUTES.get(method, ()))


# Ordinary conversation writes need a signed-in browser session (cookie, CSRF, same
# origin) but not a fresh password proof. Their real-world effects still pass
# ToolGate approval. Every other browser write keeps operation-bound verification.
SESSION_ONLY_WRITES = (
    r"/sessions",
    r"/sessions/[A-Za-z0-9_-]+/turns",
    r"/sessions/[A-Za-z0-9_-]+/fork",
    r"/turn-submissions/[A-Za-z0-9_-]+/cancel",
    # Deciding on a proposal records a preference; it grants and runs nothing.
    r"/proposals/[A-Za-z0-9_-]+/decision",
    # Browser calls use bounded transient audio or typed turns. Neither grants new
    # authority, and any external effect still requires ToolGate approval.
    r"/calls/browser",
    r"/calls/browser/call_[a-f0-9]{32}/(?:update|interrupt|end|turns)",
    r"/calls/browser/call_[a-f0-9]{32}/audio",
)


def session_only_write(method: str, path: str) -> bool:
    return method == "POST" and any(re.fullmatch(pattern, path) for pattern in SESSION_ONLY_WRITES)


# Separate owner-control credential; these capabilities are never added to the
# conversation runtime credential. Expand only alongside the corresponding UI.
OWNER_ROUTES = {
    "GET": (
        r"/search",
        r"/search/(?:settings|capabilities)",
        r"/artifacts",
        r"/collaboration/teams",
        r"/collaboration/teams/team_[0-9a-f]{32}",
        r"/collaboration/teams/team_[0-9a-f]{32}/versions",
        r"/collaboration/teams/team_[0-9a-f]{32}/versions/[1-9][0-9]{0,9}",
        r"/artifacts/artifact_[0-9a-f]{32}",
        r"/artifacts/artifact_[0-9a-f]{32}/export",
        r"/jobs",
        r"/jobs/job_[0-9a-f]{32}",
        r"/jobs/job_[0-9a-f]{32}/runs",
        r"/projects",
        r"/projects/project_[0-9a-f]{32}",
        r"/agents",
        r"/agents/(?:companion|agent_[0-9a-f]{32})",
        r"/agents/(?:companion|agent_[0-9a-f]{32})/versions",
        r"/agents/(?:companion|agent_[0-9a-f]{32})/versions/[1-9][0-9]{0,9}",
        r"/characters/companion",
        r"/characters/companion/history",
        r"/characters/companion/export",
        r"/setup/status",
        r"/setup/boundaries",
        r"/setup/models",
        r"/setup/protection",
        r"/setup/rehearsal",
        r"/setup/choices/(companion|memory|capabilities)",
        r"/setup/receipts/(boundaries|protection|rehearsal)",
        r"/system/inventory/[A-Za-z0-9_-]{16,100}",
        r"/system/inventory/configured/(?:services|containers)",
        r"/system/files/roots",
        r"/system/files/listings/[A-Za-z0-9_-]{16,100}",
        r"/calls/browser/capabilities",
        r"/calls/browser/active/[A-Za-z0-9_-]{1,200}",
        r"/calls/browser/call_[a-f0-9]{32}",
        r"/models/configuration",
        r"/sessions/[A-Za-z0-9_-]+/settings",
        r"/memory/objects",
        r"/memory/objects/[a-z]+/[A-Za-z0-9_-]+",
        r"/memory/forget/[A-Za-z0-9_.:-]+",
    ),
    "POST": (
        r"/search/settings",
        r"/artifacts",
        r"/collaboration/teams",
        r"/collaboration/teams/team_[0-9a-f]{32}/(?:update|archive|restore)",
        r"/artifacts/from-message",
        r"/artifacts/artifact_[0-9a-f]{32}/(?:versions|restore|archive)",
        r"/jobs/job_[0-9a-f]{32}/(?:state|run)",
        r"/jobs/runs/scheduled_[0-9a-f]{32}/(?:budget|provision-budget|cancel|resume|reconcile)",
        r"/projects",
        r"/projects/project_[0-9a-f]{32}/(?:update|archive|link|unlink)",
        r"/agents",
        r"/agents/(?:companion|agent_[0-9a-f]{32})/update",
        r"/agents/agent_[0-9a-f]{32}/archive",
        r"/characters/companion/(?:save|import|restore)",
        r"/setup/choices/(companion|memory|capabilities)",
        r"/setup/models",
        r"/setup/models/probe",
        r"/setup/models/activate",
        r"/setup/protection",
        r"/setup/rehearsal/(?:memory-review|approval/(?:start|resume)|finalize)",
        r"/setup/receipts/(boundaries|protection)",
        r"/system/inventory",
        r"/system/inventory/[A-Za-z0-9_-]{16,100}/resume",
        r"/system/files/listings",
        r"/system/files/listings/[A-Za-z0-9_-]{16,100}/resume",
        r"/calls/browser",
        r"/calls/browser/call_[a-f0-9]{32}/(?:update|interrupt|end|turns)",
        r"/calls/browser/call_[a-f0-9]{32}/audio",
        r"/models/configuration",
        r"/sessions/[A-Za-z0-9_-]+/settings",
        r"/proposals/passes",
        r"/memory/forget",
    ),
}


def owner_allowed(method: str, path: str) -> bool:
    return any(re.fullmatch(pattern, path) for pattern in OWNER_ROUTES.get(method, ()))
