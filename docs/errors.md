# Errors

Every refusal from a Pi capability has one shape and an HTTP status:

```json
{"detail": {"code": "not_found", "message": "Task not found."}}
```

- `code` is a stable machine word; `message` is a sentence for people. Clients branch on `code`.
- A few refusals add fields next to them, such as `current_revision` on a revision conflict for tasks,
  agents, teams and owner preferences. Each is documented on its capability page.
- A request that fails validation answers `422` with `{"detail": "Request validation failed."}`. It is
  deliberately static, so it never echoes the input back.
- Checks a route makes before the capability runs (authentication, body size, query parameters,
  malformed identities, unknown routes) answer with FastAPI's plain `{"detail": "..."}`.

## In code

A module that refuses raises its own subclass of `pi.core.errors.ServiceError(code, message, status)`,
so a traceback names where it came from. Routes do not catch it: the one handler that
`pi.core.errors.install(app)` adds turns it into the shape above. A test that builds its own `FastAPI()`
app calls `install(app)` too.

`tests/test_error_shape.py` holds one refusal from each family to the exact body.
