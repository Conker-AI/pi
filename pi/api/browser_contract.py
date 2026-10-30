"""The explicit conversation capabilities granted to the browser gateway."""

import re

RUNTIME_ROUTES = {
    "GET": (
        r"/health",
        r"/sessions",
        r"/sessions/history",
        r"/sessions/[A-Za-z0-9_-]+",
        r"/sessions/[A-Za-z0-9_-]+/thinking",
        r"/sessions/[A-Za-z0-9_-]{1,128}/observation",
        r"/sessions/[A-Za-z0-9_-]{1,128}/memory-delivery",
        r"/sessions/[A-Za-z0-9_-]{1,128}/messages/[A-Za-z0-9_-]{1,128}/memory-delivery",
        r"/sessions/[A-Za-z0-9_-]+/submissions",
        r"/turns/unreplied",
        r"/turns/[A-Za-z0-9_-]{1,128}/usage",
        r"/turn-submissions/[A-Za-z0-9_-]+",
        r"/turn-submissions/[A-Za-z0-9_-]+/stream",
        r"/approvals",
        r"/tools",
        r"/skills",
        r"/skills/[a-z0-9][a-z0-9-]{0,63}",
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
        # A webhook call; Pi checks the job's own secret, the job and the rate.
        r"/hooks/job_[a-f0-9]{32}",
        r"/sessions",
        r"/sessions/[A-Za-z0-9_-]+/turns",
        r"/sessions/[A-Za-z0-9_-]+/thinking/[A-Za-z0-9_-]+/cancel",
        r"/sessions/[A-Za-z0-9_-]+/fork",
        r"/models/auto-settings",
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
    r"/drafts/(?:new(?:/start)?|[A-Za-z0-9_-]{1,128}(?:/tasks/[A-Za-z0-9_-]{1,128})?)",
    r"/sessions/[A-Za-z0-9_-]+/thinking/[A-Za-z0-9_-]+/cancel",
    r"/sessions",
    r"/sessions/[A-Za-z0-9_-]+/turns",
    r"/sessions/[A-Za-z0-9_-]+/fork",
    r"/turn-submissions/[A-Za-z0-9_-]+/cancel",
    # An Auto settings proposal reads the typed draft and returns typed choices; it saves and
    # grants nothing, and the owner sees every value before sending.
    r"/models/auto-settings",
    # Deciding on a proposal records a preference; it grants and runs nothing.
    r"/proposals/[A-Za-z0-9_-]+/decision",
    # Declining Conker's request creates nothing; reporting the Tools draft a tool request was
    # saved as only records its id (the save itself was verified by the editor route). Drafting a
    # tool, again or from a description, only fills a card for review, and reporting it published
    # only records it (publishing was verified by ToolGate's route): none of them makes anything.
    r"/change-requests/creq_[a-f0-9]{32}/(?:decline|saved|retry|published)",
    r"/tool-drafts",
    # Dismissing an AI job's note hides one line; the chat keeps the reply.
    r"/jobs/agent/notes/note_[a-f0-9]{32}/dismiss",
    # The owner's settings document holds preferences only (look, wording, defaults): what Conker
    # may do lives in ToolGate and in documents that keep asking for the password.
    r"/owner/settings",
    # Answering Conker's question sends his message to that chat, like typing it there; any action
    # that follows passes his access layers. Dismissing sends nothing.
    r"/asks/ask_[a-f0-9]{32}/(?:answer|dismiss)",
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
        r"/drafts/new/start/[A-Za-z0-9_-]{16,128}",
        r"/drafts/(?:new|[A-Za-z0-9_-]{1,128}(?:/tasks/[A-Za-z0-9_-]{1,128})?)",
        r"/skills",
        r"/skills/[a-z0-9][a-z0-9-]{0,63}",
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
        r"/jobs/agent/notes",
        r"/change-requests",
        r"/asks",
        r"/asks/ask_[a-f0-9]{32}",
        r"/access/handled",
        r"/change-requests/creq_[a-f0-9]{32}",
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
        # Read-only: the chat's context policy and summary for the Context view. Edits, forks,
        # turn snapshots and selections stay with the recovery credential.
        r"/context/[A-Za-z0-9_-]{1,128}",
        r"/context/[A-Za-z0-9_-]{1,128}/summary",
        r"/memory/objects",
        r"/memory/objects/[a-z]+/[A-Za-z0-9_-]+",
        r"/memory/forget/[A-Za-z0-9_.:-]+",
        r"/memory/forget-pending",
        r"/memory/corrections/pending",
        r"/memory/corrections/memories/[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}",
        r"/memory/corrections/[A-Za-z0-9_-]{16,128}",
        # Read-only: plan usage in percent per window, for the dashboard's Home. Dismissing an
        # alert stays with the recovery credential.
        r"/plans/usage",
        # Read-only: which services called a model role and what it used; never any text.
        r"/inference/usage",
        # The Settings screen: the owner's settings document and his preferences.
        r"/owner/settings",
        r"/owner/preferences",
    ),
    "POST": (
        r"/drafts/(?:new(?:/start)?|[A-Za-z0-9_-]{1,128}(?:/tasks/[A-Za-z0-9_-]{1,128})?)",
        r"/skills/import",
        r"/skills/[a-z0-9][a-z0-9-]{0,63}/state",
        r"/search/settings",
        r"/artifacts",
        r"/collaboration/teams",
        r"/collaboration/teams/team_[0-9a-f]{32}/(?:update|archive|restore)",
        r"/artifacts/from-message",
        r"/artifacts/artifact_[0-9a-f]{32}/(?:versions|restore|archive)",
        r"/jobs/job_[0-9a-f]{32}/(?:state|run)",
        # An AI job only, never a raw tool target; creating one asks for the password.
        r"/jobs/agent",
        # Approving makes Conker's request real, so it asks for the password; declining doesn't.
        r"/change-requests/creq_[a-f0-9]{32}/(?:approve|decline|saved|retry|published)",
        # A tool request without a chat: it is drafted for review and makes nothing.
        r"/tool-drafts",
        # A webhook secret is shown once; making or stopping one asks for the password.
        r"/jobs/job_[0-9a-f]{32}/hook",
        r"/jobs/job_[0-9a-f]{32}/hook/revoke",
        r"/jobs/agent/notes/note_[a-f0-9]{32}/dismiss",
        r"/asks/ask_[a-f0-9]{32}/(?:answer|dismiss)",
        # The gateway, after ToolGate recorded his decision: run it, or close it. Pi grants nothing.
        r"/approvals/[A-Za-z0-9_-]{1,128}/decided",
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
        r"/memory/remember",
        r"/memory/corrections",
        r"/memory/corrections/[A-Za-z0-9_-]{16,128}/retry",
        r"/owner/settings",
        # Quiet hours, proactivity, daily budgets: saving asks for the password.
        r"/owner/preferences",
    ),
}


def owner_allowed(method: str, path: str) -> bool:
    return any(re.fullmatch(pattern, path) for pattern in OWNER_ROUTES.get(method, ()))


# Files the owner-control credential may download straight from Pi (the host CLI does). They
# stay out of OWNER_ROUTES because the gateway's control relay forwards JSON only: a browser
# download needs its own relay that passes the file, its type and its name through unchanged.
OWNER_DOWNLOADS = (
    r"/artifacts/artifact_[0-9a-f]{32}/download",
    r"/sessions/ses_[0-9a-f]{16}/messages/msg_[0-9a-f]{16}/download",
)


def owner_download_allowed(method: str, path: str) -> bool:
    return method == "GET" and any(re.fullmatch(pattern, path) for pattern in OWNER_DOWNLOADS)
