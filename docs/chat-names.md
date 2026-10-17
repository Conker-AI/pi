# Chat names

The owner can rename any chat. An owner-typed name is never changed automatically.

After a chat's first answer, if it has no name, the routing helper (Jev) names it with two to six
words. The owner can also ask Jev to name a chat at any time through the auto-name route.

What is sent to the helper: the first question and the first answer after it, each cut to 2000
characters. Messages marked `exclude` in the context policy are never sent.

Naming is refused when helpers are off for the chat (harness disabled). The naming routes are
admin-only for now.
