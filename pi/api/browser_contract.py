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


# A signed-in browser session (cookie, CSRF, same origin, an unlock window that ends after the
# owner's idle time) is enough for every browser write except the ones below: the sign-in already
# proved the password, and what a session can do is ordinary and can be undone. These keep asking
# for a password proof bound to the exact operation (ADR-0010): forgetting (cannot be undone), money
# (budgets and spending), secrets shown once, the owner's security preferences (sign-out time, daily
# budgets), where backups go, taking a job of Conker's as the owner's own, and the gateway's own
# approval hand-off. Real-world effects of everything else still pass ToolGate and the owner's
# access layers; a delete or payment approval asks for the password there (approval level 3).
PASSWORD_WRITES = (
    # A webhook call through the browser gateway; Pi also checks the job's own secret.
    r"/hooks/job_[a-f0-9]{32}",
    r"/memory/forget(?:-many)?",
    # An imported chat is deleted for good; it cannot be imported back.
    r"/imports/chats/imp_[0-9a-f]{32}/delete",
    # Chats deleted forever: Pi restarts and forgets them before it opens again.
    r"/forgetting/chats",
    # Sign-out time, quiet hours, proactivity and daily budgets.
    r"/owner/preferences",
    # A webhook secret is shown once.
    r"/jobs/job_[0-9a-f]{32}/hook",
    # From then on the job's runs count as the owner's.
    r"/jobs/job_[0-9a-f]{32}/adopt",
    r"/jobs/runs/scheduled_[0-9a-f]{32}/(?:budget|provision-budget)",
    # Where backups are written and how many are kept.
    r"/setup/protection",
    # The gateway calls this itself after ToolGate recorded the owner's decision; never the browser.
    r"/approvals/[A-Za-z0-9_-]{1,128}/decided",
)


def password_write(method: str, path: str) -> bool:
    return method == "POST" and any(re.fullmatch(pattern, path) for pattern in PASSWORD_WRITES)


def session_only_write(method: str, path: str) -> bool:
    """A browser write the signed-in session alone may make (the gateway's routes check theirs)."""
    return method == "POST" and not password_write(method, path)


# The chat features of the v1 dashboard: output settings, chat names, answer families (compare by
# retry, judge, vote, select), fact checks, branching from a message, and context editing (policy,
# summary, compaction, decisions and threads, a new chat from a reviewed summary). Ids are exact:
# a chat is ses_ plus 16 hex, a message msg_ plus 16 hex, a decision or thread dec_/thr_ plus 16
# hex, a request id 16-128 safe characters. Writes need the signed-in session only; the gateway
# checks each body against Pi's own model before relaying it (gateway/chat_bodies.py).
# Turn snapshots and context selections stay with the recovery credential.
_CHAT = r"ses_[0-9a-f]{16}"
_MESSAGE = r"msg_[0-9a-f]{16}"
_REQUEST = r"[A-Za-z0-9_-]{16,128}"
_ITEM = r"(?:dec|thr)_[0-9a-f]{16}"
CHAT_READS = (
    rf"/sessions/{_CHAT}/output",
    rf"/sessions/{_CHAT}/title",
    rf"/sessions/{_CHAT}/branch-history",
    rf"/sessions/{_CHAT}/response-families/{_MESSAGE}",
    rf"/sessions/{_CHAT}/response-families/{_MESSAGE}/judgements",
    rf"/sessions/{_CHAT}/response-families/{_MESSAGE}/votes",
    rf"/sessions/{_CHAT}/response-retries/{_REQUEST}",
    rf"/sessions/{_CHAT}/messages/{_MESSAGE}/fact-checks",
    rf"/context/{_CHAT}/items",
    rf"/context/{_CHAT}/compaction",
)
CHAT_WRITES = (
    rf"/sessions/{_CHAT}/output",
    rf"/sessions/{_CHAT}/title",
    rf"/sessions/{_CHAT}/title/auto",
    # Archive or restore up to 100 chats at once (a list state; nothing is deleted).
    r"/sessions/archive",
    # Move up to 100 chats to the trash or restore them (no password; deleting forever asks).
    r"/sessions/trash",
    rf"/sessions/{_CHAT}/messages/{_MESSAGE}/retry",
    rf"/sessions/{_CHAT}/messages/{_MESSAGE}/fact-check",
    rf"/sessions/{_CHAT}/messages/{_MESSAGE}/fork",
    rf"/sessions/{_CHAT}/response-families/{_MESSAGE}/select",
    rf"/sessions/{_CHAT}/response-families/{_MESSAGE}/judge",
    rf"/sessions/{_CHAT}/response-families/{_MESSAGE}/vote",
    rf"/context/{_CHAT}",
    rf"/context/{_CHAT}/fork",
    rf"/context/{_CHAT}/summary",
    rf"/context/{_CHAT}/summary/restore",
    rf"/context/{_CHAT}/summary/draft",
    rf"/context/{_CHAT}/compact",
    rf"/context/{_CHAT}/items",
    rf"/context/{_CHAT}/items/{_ITEM}/state",
)


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
        # The chat's context policy and summary for the Context view.
        r"/context/[A-Za-z0-9_-]{1,128}",
        r"/context/[A-Za-z0-9_-]{1,128}/summary",
        *CHAT_READS,
        r"/memory/objects",
        r"/memory/objects/[a-z]+/[A-Za-z0-9_-]+",
        r"/memory/forget/[A-Za-z0-9_.:-]+",
        r"/memory/forget-pending",
        # Read-only: what Conker believes about the owner, one page at a time.
        r"/memory/beliefs",
        r"/insights/tomorrow",  # read-only: what Conker will start on its own soon (R-U10)
        # Read-only: counts of what asked for the owner and what did not, never his words.
        r"/insights/quiet",
        # Read-only: duplicates, contradictions, expired and unused memories to review.
        r"/memory/upkeep",
        # Read-only: the answers that used one memory (ids and chat titles, no words).
        r"/memory/[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}/uses",
        # Read-only: promises across visible chats (R-M7). Writes stay recovery-key only until
        # the dashboard has a screen for them.
        r"/open-loops",
        r"/memory/corrections/pending",
        r"/memory/corrections/memories/[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}",
        r"/memory/corrections/[A-Za-z0-9_-]{16,128}",
        # Read-only: plan usage in percent per window, for the dashboard's Home. Dismissing an
        # alert stays with the recovery credential.
        r"/plans/usage",
        # Read-only: Pi's self-check, each check's last status and the open alerts.
        r"/self-check",
        # Conker updates: the release on offer and how updates ended. The host reports them with
        # the recovery credential; the update itself is a gateway host action (password).
        r"/updates",
        # Read-only: which services called a model role and what it used; never any text.
        r"/inference/usage",
        # Read-only: today's paid model calls against the daily cap; counts only.
        r"/inference/spending",
        # The Settings screen: the owner's settings document and his preferences.
        r"/owner/settings",
        r"/owner/preferences",
        # Read-only: imports from ChatGPT and Claude and the archived chats they made.
        r"/imports",
        r"/imports/chats",
        r"/imports/chats/imp_[0-9a-f]{32}",
        # What deleting chats forever reaches (counts only) and the queued ones.
        r"/forgetting/chats",
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
        # An AI job only, never a raw tool target; what it may do follows his access layers.
        r"/jobs/agent",
        # Approving makes Conker's request real: an AI job, a tool draft or a memory, each undoable.
        r"/change-requests/creq_[a-f0-9]{32}/(?:approve|decline|saved|retry|published)",
        # A tool request without a chat: it is drafted for review and makes nothing.
        r"/tool-drafts",
        # A webhook secret is shown once, so making one asks for the password; stopping one doesn't.
        r"/jobs/job_[0-9a-f]{32}/hook",
        r"/jobs/job_[0-9a-f]{32}/hook/revoke",
        # Taking a job Conker made as his own: from then on its runs count as his.
        r"/jobs/job_[0-9a-f]{32}/adopt",
        r"/jobs/agent/notes/note_[a-f0-9]{32}/dismiss",
        r"/asks/ask_[a-f0-9]{32}/(?:answer|dismiss)",
        # The gateway, after ToolGate recorded the owner's decision: run it, or close it. Pi grants nothing.
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
        r"/updates/offers/[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}/dismiss",
        r"/updates/results/[0-9a-f]{32}/dismiss",
        r"/system/inventory",
        r"/system/inventory/[A-Za-z0-9_-]{16,100}/resume",
        r"/system/files/listings",
        r"/system/files/listings/[A-Za-z0-9_-]{16,100}/resume",
        r"/calls/browser",
        r"/calls/browser/call_[a-f0-9]{32}/(?:update|interrupt|end|turns)",
        r"/calls/browser/call_[a-f0-9]{32}/audio",
        r"/models/configuration",
        r"/sessions/[A-Za-z0-9_-]+/settings",
        # Next-message suggestions for one chat: Jev reads its last exchange and saves nothing.
        r"/sessions/[A-Za-z0-9_-]{1,128}/suggestions",
        # Jev rewrites an unsent draft; nothing is saved or sent, so the session alone is enough.
        r"/prompts/enhance",
        *CHAT_WRITES,
        r"/proposals/passes",
        # Hiding one self-check alert; the session alone is enough.
        r"/self-check/alerts/sc_[0-9a-f]{32}/dismiss",
        # Hiding one "answers used a memory you changed" notice (R-U3); the session alone is enough.
        r"/memory/recall-notices/mrn_[0-9a-f]{32}/dismiss",
        r"/memory/forget(?:-many)?",
        r"/memory/remember",
        r"/memory/corrections",
        r"/memory/corrections/[A-Za-z0-9_-]{16,128}/retry",
        r"/owner/settings",
        # Sign-out time, quiet hours, proactivity, daily budgets: saving asks for the password.
        r"/owner/preferences",
        # A ChatGPT or Claude export: a preview saves nothing; an import (named by the previewed
        # file's digest) saves read-only chats and offers his words to memory as outside content.
        # Both need the signed-in session only.
        r"/imports/(?:chatgpt|claude)/(?:preview|[0-9a-f]{64})",
        # Deleting one imported chat for good asks for the password (PASSWORD_WRITES).
        r"/imports/chats/imp_[0-9a-f]{32}/delete",
        # Deleting chats forever asks for the password (PASSWORD_WRITES).
        r"/forgetting/chats",
    ),
}


def owner_allowed(method: str, path: str) -> bool:
    return any(re.fullmatch(pattern, path) for pattern in OWNER_ROUTES.get(method, ()))


# Files the owner-control credential may download from Pi. They stay out of OWNER_ROUTES because
# the gateway's control relay forwards JSON only: the browser gets them through the gateway's file
# relay (gateway/file_routes.py), which streams the file with its checked type and name; the host
# CLI fetches them straight from Pi.
OWNER_DOWNLOADS = (
    r"/artifacts/artifact_[0-9a-f]{32}/download",
    r"/sessions/ses_[0-9a-f]{16}/messages/msg_[0-9a-f]{16}/download",
)


def owner_download_allowed(method: str, path: str) -> bool:
    return method == "GET" and any(re.fullmatch(pattern, path) for pattern in OWNER_DOWNLOADS)
