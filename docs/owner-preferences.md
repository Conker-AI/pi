# Owner preferences

`GET /owner/preferences` returns `{revision, preferences}`. `POST /owner/preferences`
accepts `{expected_revision, preferences}` and returns the saved document with an
incremented revision. Both require the existing `X-Pi-Key` administration credential.
Gateway runtime credentials are denied; browser wiring is deliberately deferred.

The preference document matches the frontend configuration fields:

```json
{
  "quietHours": {
    "enabled": true, "start": "22:00", "end": "07:00",
    "timeZone": "Asia/Jerusalem", "urgentExceptions": false
  },
  "urgency": "meaningful",
  "dailyBudget": {"suggestions": 4, "researchMinutes": 30, "costCents": 0},
  "idleTimeoutMinutes": 15
}
```

Fields are strict: unknown fields and coercions are rejected. Quiet-hour times use
24-hour HH:mm and an available IANA zone; enabled start and end must differ.
Urgency accepts `meaningful`, `urgent_only`, or `off`. Integer daily ceilings are
0–100 suggestions, 0–1440 research minutes, and 0–100000 cents. Idle timeout accepts
0, 5, 15, 30, or 60 minutes. Validation failures return 422. Stale revisions return
409 with `detail.code=revision_conflict` and `detail.current_revision`; reload and
reconcile before saving. SQLite serializes concurrent updates and retains the
configuration across restart. Saving never dispatches a turn or changes grants.

`pi.owner_preferences` provides pure `quiet_now`, `local_day`, and
`admission_reasons` policy functions. Quiet windows include the start and exclude
the end, support overnight windows, and follow local wall time across DST skips
and repeats. An aware timestamp is required. Urgent exceptions bypass quiet hours
only; they do not bypass `off` or daily ceilings. Usage must identify the same local
calendar day. Positive requests exceed a zero ceiling; zero-cost work can pass a
zero cost ceiling. Existing usage plus proposed usage is checked against each cap.

These functions are **not scheduler enforcement or permission authorization**.
There is no scheduler admission hook, atomic budget reservation ledger, notification
delivery, or idle-lock enforcement in this package. A future scheduler must supply
authoritative same-day usage and reserve work atomically before dispatch. Gateway
idle enforcement remains separate. Timezone data is supplied by the pinned `tzdata`
dependency when the operating system has no IANA database.

Validation (isolated temporary stores; no live services):

```sh
python -m pytest tests/test_owner_preferences.py tests/test_store.py tests/test_tasks.py -q
git diff --check
```
