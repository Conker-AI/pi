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
3. **Outside content:** once the turn can read text the owner did not write, a loosening below
   the tool's default goes one step back (full becomes ai, ai becomes ask), his words no longer
   count, and Conker's own Keep actions go to the reviewer. Outside content is: a result from a
   tool that is not Conker's own (in the chat or a message it inherited), a webhook payload (once
   one arrives, for the whole chat and its forks; none of that chat's messages goes to memory),
   memories recalled for this turn, an attachment in the chat, a project source, and his answer
   to one of Conker's questions (its question and options were the AI's).

**His words count:** when an action would ask, is not on the floor and no outside content is in the
chat, the reviewer checks whether his own message asks for exactly this action; if so it runs. Not
for scheduling: a job made from his words would still run instructions the model wrote, so it waits
for his approval unless his settings loosen it. In a run of the owner's own AI job only the job's
saved instructions count: the preamble, the run's date and time and why it started are Pi's.
A run of an AI job Conker made on its own has no words of his at all.

**The reviewer** (`pi/turns/reviewer.py`) runs on its own model role, `reviewer`, when he set one
up (otherwise the answer model). In AI decides the hard stop comes from what he set, not from a
model: a tool whose Inbox card he set to send, publish, ship, delete or pay (ToolGate's card
template, read from the tool list when ToolGate lists it) always asks him, and no model is called.
The reviewer also answers two questions: does the action reach a real person, and can it not be
undone? A yes, or anything but a clear no, is one more reason to ask; a no never lets through
what his card says asks, because the verdict and those answers come from one model call that the
same text can steer. A tool from an external MCP server reaches the reviewer by id and name only:
its description came from that server. His own typed words and Full access are his choices and
are not overruled (mark a tool "always ask" for that); the floor and outside content still apply.
The reviewer is told when the run has read outside content.

**Enforcement:** Pi tells ToolGate the outcome (`decision`), and ToolGate honours "run" only when the
owner made Pi's key a policy delegate (`GET /v2/agent/status` reports it; Pi asks once per turn and
decides nothing for ToolGate tools otherwise) and never on the floor.

**Conker's own tools** follow the same layers: remembering is Keep (allowed by default, done at
once); scheduling asks by default (`owner_confirmation`), and his setting for an agent or a chat
can loosen it; a tool draft always waits for him in the Inbox (no reviewer is asked: it makes
nothing until he publishes it). Every one is still recorded as a change request. A private chat
(memory off, or inheriting it) is never offered remembering or scheduling, and a run of an AI job
is never offered scheduling or a tool draft; both are refused again if called. See
docs/change-requests.md.

**Handled:** every action that ran without asking him where the tool alone would have asked (and
every Keep action of Conker's) is recorded; `GET /access/handled` (owner) lists the last week with
who decided and why. Forgetting a chat removes its entries.
