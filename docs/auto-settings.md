# Auto settings

In Auto mode the composer asks Pi for settings before the owner sends a message. Runtime
`POST /models/auto-settings` takes `{text, session_id?, harness_disabled?}` and returns:

```json
{"complexity": "hard", "think": true, "research": "web",
 "reasons": {"complexity": "...", "think": "...", "research": "..."},
 "modelId": "routing-model-id", "configurationRevision": 3}
```

The routing role's model (for example Jev) makes the proposal. The owner sees every value with its
reason and may change any of them; the composer then shows Custom. Sending uses the values on
screen, so the proposal itself decides nothing.

Rules:

- The request reads only the typed draft. It saves nothing and grants nothing; privacy, model
  eligibility, budgets and ToolGate apply when the message is sent.
- Exactly three settings plus one reason each (at most 160 characters). Unknown keys, unknown values,
  non-boolean `think` or missing reasons fail closed with 503; the owner chooses the settings.
- No helpers, no proposal: a chat (or a new-chat draft) with harness processing disabled gets 409,
  and no model is called. An unknown or forgotten conversation gets 404; no model catalogue gets 503.
- The browser may call it with a signed-in session and no fresh password proof, like starting a turn.

Complexity has two levels with a real effect. **Hard** sends `is_analysis` with the turn: the tier
router escalates with reason `analysis`, and a catalogue routing model receives
`ownerHints: {"complexity": "hard"}` beside the task. **Easy** is the ordinary route. A middle level
arrives when routing has a middle tier.

```sh
python -m pytest tests/test_auto_settings.py -q
```
