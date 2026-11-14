# Every Inbox kind (2026-11-12)

`GET /inbox` (`pi/agents/inbox.py:inbox`) builds "Needs you" from eight producers. Each has its
own table, routes and rules. This page lists them, checked against the code on this date, so a
new feature can reuse one instead of adding a ninth. See [inbox.md](inbox.md) for the view itself.

"Handled" (kinds `job`, `task`, `run`, `plan-reset`) and "Upcoming" (`job`, `task`) only report;
nothing there waits for the owner, so they are not rows below.

## The table

| Mechanism (module / table) | Inbox kind | Who creates it | Owner's answers (route) | Expiry | Undo | CLI verb | Caps | Forgotten with chat |
|---|---|---|---|---|---|---|---|---|
| ToolGate approvals (ToolGate request; Pi's parked turn, `turns.status='awaiting_approval'`, `store.awaiting_approval`) | `approval` | ToolGate, when a set tool is called by Conker in a chat or an AI job's turn | Do it / Don't / Change: ToolGate `POST /v2/owner/requests/{id}/decision` through the gateway, which then calls Pi `POST /approvals/{id}/decided` (`api/routes/answers.py:decided`) | ToolGate's `expires_at`, kept in `turns.approval_expires_at`; 5 min after it Pi ends the turn (`turns/decision_sweep.py:expire`: the chat says nothing was done) and Handled lists `approval-expired` | none | `conker inspect approvals`, `conker apply approvals` (`gateway/mutation.py`) | none in Pi | yes: hidden by `awaiting_approval`, withdrawn in ToolGate by `actions/withdrawals.py:queue` |
| Change requests (`agents/change_requests.py`, `change_requests`; kinds `ai_job`, `tool_draft`, `memory`) | `change-request` | Conker in chat (`conker.schedule`, `conker.draft_tool`, `conker.remember`); the owner (`POST /tool-drafts`) | approve (signed-in session), decline (+ `reply`), saved, retry, published: `POST /change-requests/{id}/...` | none | none on the request; a Keep result is undone by forgetting its chat (`agents/keep.py`) | `conker inspect change-requests`, `conker apply requests`, `conker tool answer` | 3 open per chat, 20 overall (`file`); the owner's own are not limited | yes: `change_requests.redact` |
| Questions (`agents/asks.py`, `owner_asks`) | `ask` | Conker in chat (`conker.ask`) | answer (an option or his words), dismiss: `POST /asks/{id}/answer`, `/dismiss` | none | none | `conker inspect asks`, `conker apply asks` | 3 open per chat, 20 overall (`asks.file`) | yes: `asks.redact` |
| Companion suggestions (`schedule/proposals.py`, `proposals`; timer `proposal_worker.py`) | `suggestion` | background worker, a `proposals` model pass over the owner's messages | accept / decline / never: `POST /proposals/{id}/decision`; accepting records a decision only (`grantsExecutionAuthority: false`) | none; a decline hides the same idea 30 days, never for good | none (`proposals_decided_once` trigger) | `conker inspect proposals`, `conker apply proposals` | 3 per pass and the daily suggestion budget (`run_pass`) | yes: every proposal from a pass that read the chat, `proposals.redact` |
| Plan alerts (`owner/plan_alerts.py`, `notices` kind `plan`) | `plan` | background worker (`PlanAlertWorker`) reading subscription usage | dismiss: `POST /plans/alerts/{id}/dismiss` (recovery key only; not in the browser contract) | ends when the plan window resets (`record`) | none | `conker limits --dismiss ID` | one open alert per plan window (unique index) | n/a: no chat |
| AI job notes (`schedule/agent_jobs.py`, `notices` kind `agent-job`) | `agent-job` | job run: the AI job's reply had something for the owner (`_note`) | dismiss: `POST /jobs/agent/notes/{id}/dismiss`; the card links to the job's chat | none | none | none found | one per run (`run_id UNIQUE`) | yes: `agent_jobs.redact` |
| Memory recall notices (`conversation/recall_notices.py`, `notices` kind `memory-recall`) | `memory-recall` | a memory correction or forget finishing (`edits._finish`, `forget.forget`), only when earlier answers used the memory (`memory_uses`) | dismiss: `POST /memory/recall-notices/{id}/dismiss` (owner session or admin key); the card links to each chat; the uses are listed by `GET /memory/{id}/uses` | none | none | `conker memory used ID` lists the uses | one per change request id (`notices` unique on kind and subject) | no chat id is stored: the row holds the memory id, the change and the revision; the counts are read from `memory_uses` in visible chats, so a forgotten or Trashed chat drops out, and a notice that reaches nothing is not shown |
| Attention (`agents/continuity.py`, `continuity_order` + `continuity_ack`) | `attention` | Pi, from run, task and job-run status events (blocked, failed, awaiting approval or budget, acted with no reply, outcome unknown, interrupted) | acknowledge: `POST /continuity/seen` (admin key; no browser route or dashboard caller found). A job run's approval is answered by its ToolGate card (merge 5) | replaced by a later status of the same run or task (`briefing`) | none | none found | none (Inbox list cut at 50) | hidden, not deleted: `continuity._eligible` |

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

1. **Plan alerts and AI job notes.** DONE 2026-11-13: one `notices` table (`pi/owner/notices.py`:
   kind, subject, title, detail, session_id, facts, created_at, reset_at, dismissed_at), filled by
   migration 0006 from `plan_alerts` and `agent_job_notes` with their ids kept, which then go.
   One partial unique index, (kind, subject) while `reset_at` is NULL, keeps both rules: one
   open alert per plan window, one note per run. Routes, item shapes and auth are unchanged, so
   the dashboard cards and `conker limits --dismiss` need no change. A shared
   `/notices` route waits until the dashboard reads the Inbox; then these two become its
   aliases. Self-check alerts (`self_check_alerts`) have the same shape and are the next kind.
2. **Memory proposals.** DONE 2026-11-12: retired. No code, screen or CLI created or answered
   one; only the admin-key route did. Pi no longer lists kind `memory` (`_needs_memory_items` is
   gone) and the `/memory-proposals` router is removed; the table stays for forgetting, like
   `retired_workflows`. Left in Conker: the `conker inbox` marker for kind `memory`
   (`scripts/conker_inbox.py`) and the generated `pi.d.ts` routes, removed at the next Pi pin.
3. **Questions into change requests.** NOT DOING (decided 2026-11-13). They look alike (open,
   then answered or dismissed; 3 per chat, 20 overall; forgotten with the chat), but an answer
   delivers his words to the chat while an approval makes a thing, and `owner_asks.answer_request`
   is what lets `turns/access.py` tell his words from a tapped option. Moving or dropping those
   rows would make old answer turns count the AI's question as his request. The premise was also
   stale: approving any request already needs only the session (no kind asks the password).
   The mapping found gaps on main instead, fixed: change requests from forgotten or trashed chats
   were listed and could be approved; a client that left out `chosen` turned a tapped option into
   his words (Pi now marks any exact option as chosen); an answer turn whose link was gone counted
   the whole message as his (now none of it); an unknown kind fell through to the memory branch.
4. **The two "proposal" mechanisms are not the same shape.** Memory proposals are an exact text
   change to one MemoryGate record at a known revision, with apply, conflict and an unknown
   outcome to reconcile. Companion suggestions are a model's idea with no chat, cited messages,
   a fingerprint, and accept, decline or never, where accept makes nothing. They share only the
   word. Merging them with each other buys nothing. A suggestion could become a change request
   whose accept files a real `ai_job` request, which gives Accept a meaning. Medium to large:
   the pass ledger, the never-list and 30-day suppression, the pass-window forgetting rule, the
   dashboard proposals panel and `conker apply proposals` all move. NOT DOING as a merge
   (2026-11-13): with memory proposals retired (merge 2) only suggestions are left, so there is
   nothing to merge; giving Accept a meaning is a product change, not a duplicate.
5. **Attention and approvals overlap.** NOT A MERGE (checked 2026-11-13). The owner meets one
   card: the dashboard lists ToolGate's open requests whoever asked (`owner_channel`), so a tool
   cron job's request is the same approval card as a chat's, and his answer reaches the run
   (`turns/answers.py` `waiting` matches the run's receipt; approve resumes it with the request,
   anything else cancels it; test `test_approving_a_tool_jobs_request_resumes_that_run_with_it`).
   The Jobs screen also says "Waiting for approval". What differs is only the label in Pi's
   admin-key `GET /inbox`, which the dashboard does not read (`conker inbox`, dev workspace,
   prints it as attention). Nothing in the turn loop, job worker or ledger needs to change.
   Left as their own items, all fixed since: cancelling a waiting run now withdraws its ToolGate
   request and a parked AI-job turn holds the next run back (pi#211); a decision made with
   ToolGate's admin key reaches Pi through the decision sweep (docs/inbox-answers.md). Turn approvals from a trashed chat still show on purpose (L-06: an
   approval waiting for an answer stays reachable).

### Also fixed while mapping (2026-11-13)
- Suggestions from a forgotten or trashed chat were listed in needs-you with the chat's words in
  the title (`proposals.HIDDEN` now filters them).
- An AI job Conker kept from a hidden chat was named in Upcoming and Handled; it is left out
  until the owner adopts it (createdBy owner), as in the jobs list. A turned-off job is no longer
  Upcoming. `tests/test_hidden_chat_never_leaks.py` now reads the Inbox too.
