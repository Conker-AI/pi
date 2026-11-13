# Every Inbox kind (2026-11-12)

`GET /inbox` (`pi/agents/inbox.py:inbox`) builds "Needs you" from seven producers. Each has its
own table, routes and rules. This page lists them, checked against the code on this date, so a
new feature can reuse one instead of adding an eighth. See [inbox.md](inbox.md) for the view itself.

"Handled" (kinds `job`, `task`, `run`, `plan-reset`) and "Upcoming" (`job`, `task`) only report;
nothing there waits for the owner, so they are not rows below.

## The table

| Mechanism (module / table) | Inbox kind | Who creates it | Owner's answers (route) | Expiry | Undo | CLI verb | Caps | Forgotten with chat |
|---|---|---|---|---|---|---|---|---|
| ToolGate approvals (ToolGate request; Pi's parked turn, `turns.status='awaiting_approval'`, `store.awaiting_approval`) | `approval` | ToolGate, when a set tool is called by Conker in a chat or an AI job's turn | Do it / Don't / Change: ToolGate `POST /v2/owner/requests/{id}/decision` through the gateway, which then calls Pi `POST /approvals/{id}/decided` (`api/routes/answers.py:decided`) | ToolGate's `expires_at`, kept in `turns.approval_expires_at` | none | `conker inspect approvals`, `conker apply approvals` (`gateway/mutation.py`) | none in Pi | yes: hidden by `awaiting_approval`, withdrawn in ToolGate by `actions/withdrawals.py:queue` |
| Change requests (`agents/change_requests.py`, `change_requests`; kinds `ai_job`, `tool_draft`, `memory`) | `change-request` | Conker in chat (`conker.schedule`, `conker.draft_tool`, `conker.remember`); the owner (`POST /tool-drafts`) | approve (password), decline (+ `reply`), saved, retry, published: `POST /change-requests/{id}/...` | none | none on the request; a Keep result is undone by forgetting its chat (`agents/keep.py`) | `conker inspect change-requests`, `conker apply requests`, `conker tool answer` | 3 open per chat, 20 overall (`file`); the owner's own are not limited | yes: `change_requests.redact` |
| Questions (`agents/asks.py`, `owner_asks`) | `ask` | Conker in chat (`conker.ask`) | answer (an option or his words), dismiss: `POST /asks/{id}/answer`, `/dismiss` | none | none | `conker inspect asks`, `conker apply asks` | 3 open per chat, 20 overall (`asks.file`) | yes: `asks.redact` |
| Companion suggestions (`schedule/proposals.py`, `proposals`; timer `proposal_worker.py`) | `suggestion` | background worker, a `proposals` model pass over the owner's messages | accept / decline / never: `POST /proposals/{id}/decision`; accepting records a decision only (`grantsExecutionAuthority: false`) | none; a decline hides the same idea 30 days, never for good | none (`proposals_decided_once` trigger) | `conker inspect proposals`, `conker apply proposals` | 3 per pass and the daily suggestion budget (`run_pass`) | yes: every proposal from a pass that read the chat, `proposals.redact` |
| Plan alerts (`owner/plan_alerts.py`, `plan_alerts`) | `plan` | background worker (`PlanAlertWorker`) reading subscription usage | dismiss: `POST /plans/alerts/{id}/dismiss` (owner key; not in the browser contract) | ends when the plan window resets (`record`) | none | `conker limits --dismiss ID` | one open alert per plan window (unique index) | n/a: no chat |
| AI job notes (`schedule/agent_jobs.py`, `agent_job_notes`) | `agent-job` | job run: the AI job's reply had something for the owner (`_note`) | dismiss: `POST /jobs/agent/notes/{id}/dismiss`; the card links to the job's chat | none | none | none found | one per run (`run_id UNIQUE`) | yes: `agent_jobs.redact` |
| Attention (`agents/continuity.py`, `continuity_order` + `continuity_ack`) | `attention` | Pi, from run, task and job-run status events (blocked, failed, awaiting approval or budget, acted with no reply, outcome unknown, interrupted) | acknowledge: `POST /continuity/seen` (admin key; no browser route or dashboard caller found) | replaced by a later status of the same run or task (`briefing`) | none | none found | none (Inbox list cut at 50) | hidden, not deleted: `continuity._eligible` |

Not Inbox rows, though named like one:

- Import offers (`memory/import_offers.py`) send imported words to a MemoryGate listener. Nothing
  waits for the owner, and `inbox` does not read them.
- Workflow offers and workflow proposals (`agents/retired_workflows.py`) are retired (2026-10-30).
  The tables stay only so forgetting a chat clears old rows.
- Memory proposals (`memory/proposals.py`, kind `memory`) are retired (2026-11-12, merge 2). The
  routes and the Inbox item are gone; the `memory_proposals` table stays only so forgetting a chat
  (`proposals.redact`) or a memory (`proposals.redact_memory`) scrubs old rows.
- Memory edits by hand (`/memory/corrections`, `conker inspect memory-edit-pending`) are the
  owner's own corrections, not shown in the Inbox.

## The rule

A new thing that waits for the owner reuses an existing row instead of adding a mechanism.
Prefer change requests: add a kind to `KINDS` (a pending branch, `feat/agent-create`, adds `agent`
and `team`). It already has the Inbox card, the per-chat and overall caps, the CLI verb,
forgetting with the chat and Keep under the access layers. A one-line heads-up with only a
dismiss button reuses a notice row (see merge 1). An outward action is always a ToolGate
approval; that row is never merged with the others.

## Duplicates, smallest merge first

1. **Plan alerts and AI job notes.** Both are a line, a link and Dismiss, with no undo and no
   answer. One notices table (kind, title, detail, link, dismissed_at) would serve both; plan
   alerts keep their per-window reset as a column. Touches `plan_alerts`, `agent_job_notes`,
   routes `/plans/alerts/{id}/dismiss` and `/jobs/agent/notes/{id}/dismiss`, the browser
   contract, dashboard `lib/gateway/jobs.ts` and the Inbox cards, `conker limits --dismiss`.
   Small: one migration, two routes kept as aliases for a release.
2. **Memory proposals.** DONE 2026-11-12: retired. No code, screen or CLI created or answered
   one; only the admin-key route did. Pi no longer lists kind `memory` (`_needs_memory_items` is
   gone) and the `/memory-proposals` router is removed; the table stays for forgetting, like
   `retired_workflows`. Left in Conker: the `conker inbox` marker for kind `memory`
   (`scripts/conker_inbox.py`) and the generated `pi.d.ts` routes, removed at the next Pi pin.
3. **Questions into change requests.** Same life (open, then answered or dismissed), same caps
   (3 per chat, 20 overall), same forgetting, both from Conker in chat. A question becomes kind
   `question` with `{question, options}`; answer is approve with text. Touches `owner_asks`
   (its `answer_request` column feeds `turns/answers.py` and `asks.answer_words`, which
   `turns/access.py` uses to tell his words from tapped options), routes `/asks/*`, the browser
   contract, `gateway/mutation.py` (`asks`), the dashboard ask card, `conker apply asks`.
   Medium. Answering must keep needing only the session, while approving a job asks for the
   password: the approval level has to follow the kind.
4. **The two "proposal" mechanisms are not the same shape.** Memory proposals are an exact text
   change to one MemoryGate record at a known revision, with apply, conflict and an unknown
   outcome to reconcile. Companion suggestions are a model's idea with no chat, cited messages,
   a fingerprint, and accept, decline or never, where accept makes nothing. They share only the
   word. Merging them with each other buys nothing. A suggestion could become a change request
   whose accept files a real `ai_job` request, which gives Accept a meaning. Medium to large:
   the pass ledger, the never-list and 30-day suppression, the pass-window forgetting rule, the
   dashboard proposals panel and `conker apply proposals` all move.
5. **Attention and approvals overlap.** A chat turn waiting on ToolGate is an `approval`; a
   tool cron job's run waiting on ToolGate shows as `attention` with status
   `awaiting_approval`. Attention also has no browser or CLI answer. Large: it touches the
   turn loop, the job worker and the continuity ledger. Do it last, and keep the ToolGate
   decision path unchanged (security-critical).
