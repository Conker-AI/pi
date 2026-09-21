# Pi database recovery drill

`python -m pytest tests/test_backend_restore.py -q` exercises restoration into a
new temporary database using SQLite's backup API. It never opens an owner database
or invokes external services.

The drill verifies transcript identity, attachment bytes/hash, unsent drafts, and
completed submission replay without another model call. A second scenario restores
a preparation interrupted before dispatch and an uncertain scheduled effect:
startup recovery does not resubmit the preparation, and the scheduler does not
retry the effect or admit overlapping work.

A third scenario snapshots the gateway auth database, demonstrates that restored
cookies remain valid until revocation, then applies the existing host `revoke-all`
primitive. After reopening the database, old cookies and verification proofs fail,
proof rows are absent, and the original password can create a fresh session.
This step must run before exposing a restored gateway; normal startup deliberately
does not revoke sessions on every restart.

This is evidence for Pi snapshot recovery and explicit auth revocation, not a production restore utility or a
cross-service recovery guarantee. Use SQLite backup tooling rather than copying
only a running database's main file; committed pages may still be in its WAL.

Before a real restore, stop workers and isolate outbound effects. Preserve the
current database and reconcile later ToolGate receipts with the chosen snapshot.
A snapshot taken *before* an effect cannot establish that the effect never ran.
Do not enable schedulers simply because the snapshot passes an integrity check.
Replay subsequent forgetting receipts before exposing restored conversations:
older backups can contain content deliberately removed later. Gateway sessions
must be invalidated separately, and MemoryGate/ToolGate data and vault recovery
must be handled by their service owners. These coordinated recovery procedures
remain an acceptance gap; this test does not close them.
