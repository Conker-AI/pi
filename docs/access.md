# Access: chat > agent > tool, three guards (2026-10-26)

Conker should be automatic but safe: the owner is not asked about what his own settings already
answer. Each action resolves (`pi/turns/access.py`) to **allow**, **ai** (Pi's reviewer decides;
unsure or unreadable means ask) or **ask** (one tap in the Inbox).

| Layer (more specific wins) | Where it lives |
|---|---|
| This chat's switch: `default`, `ask`, `ai`, `full` | session settings `access` |
| The agent's level per tool: `allow`, `ai`, `ask` | agent profile `toolAccess` |
| The tool's own default | ToolGate `authorization` (`auto` allow, `ai_review` ai, `owner_confirmation` ask) |

The chat switch only touches what is not free: `full` lets anything that would ask or go to the AI
run; `ai` sends what would ask to the reviewer; `ask` undoes loosening below the tool's default and
leaves nothing to the AI. Reads that are free by default stay free.

Guards no layer removes:
1. **Visibility:** a tool outside the agent's toolkit is never offered, and ToolGate answers 404 for
   a tool outside the key's scope.
2. **Floor:** a tool ToolGate lists as `sensitive` always asks; ToolGate enforces it again.
3. **Outside content:** once the chat holds a result from a tool that is not Conker's own, or a
   webhook payload, a loosening below the tool's default goes one step back (full becomes ai, ai
   becomes ask), his words no longer count, and Conker's own Keep actions (remember, schedule) go
   to the reviewer.

**His words count:** when an action would ask, is not on the floor and no outside content is in the
chat, the reviewer checks whether his own message asks for exactly this action; if so it runs.

**The reviewer** (`pi/turns/reviewer.py`) runs on its own model role, `reviewer`, when he set one
up (otherwise the answer model). In AI decides it also answers two questions: does the action reach
a real person (send, reply, post, invite, share), and can it not be undone (delete for good, pay,
buy, submit, cancel, publish)? A yes, or anything but a clear no, means ask him, whatever its
verdict; Pi enforces that from the two answers, so a reviewer talked into approving cannot let such
an action through. His own typed words and Full access are his choices and are not overruled
(mark a tool "always ask" for that); the floor and outside content still apply. The reviewer is
told when the run has read outside content.

**Enforcement:** Pi tells ToolGate the outcome (`decision`), and ToolGate honours "run" only when the
owner made Pi's key a policy delegate (`GET /v2/agent/status` reports it; Pi asks once per turn and
decides nothing for ToolGate tools otherwise) and never on the floor.

**Conker's own tools** follow the same layers: remembering and scheduling are Keep (allowed by
default, done at once), a tool draft always waits for him in the Inbox (no reviewer is asked: it
makes nothing until he publishes it). Every one is still recorded as a change request.

**Handled:** every action that ran without asking him where the tool alone would have asked (and
every Keep action of Conker's) is recorded; `GET /access/handled` (owner) lists the last week with
who decided and why. Forgetting a chat removes its entries.
