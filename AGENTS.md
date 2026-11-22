# Working in pi

Part of the Conker workspace; read the workspace `AGENTS.md` (two levels up from `gates/pi`) first. It holds the shared rules for every model.

`pi` is turns, sessions, agents, schedule (Python/FastAPI). It is a headless gate with its own repo, CI and release tags.

## Rules
- Branch per unit of work; never commit to `main`, never force-push. Merge when CI is green.
- Keep to the files the task allows. No scope creep.
- Security-critical code (approvals, spending, auth, vault, forgetting, access policy, backup) is written by Claude, with a real test.
- The repo is public: no keys, passwords, hostnames or Tailscale addresses.
- Never edit `CHANGELOG.md` in a feature PR unless the task says so.

## Checks (run before a PR; CI runs the same)
- Tests: `python -m pytest tests/ --ignore=tests/test_module_contract.py -q -n auto`
- Other checks: `python scripts/check_file_sizes.py; python scripts/check_package_boundaries.py; python scripts/check_types.py; python scripts/export_openapi.py --check`
- Lint and format: `ruff check .` and `ruff format --check .`; hooks in `.pre-commit-config.yaml`.

## Limits
- Every source file stays under 600 lines (`file-size-allowlist.txt` may only shrink).
- `package-boundaries.txt` lists the allowed cross-package edges in `pi/`; the list only shrinks. Nothing imports `pi.api`; no loose modules.
- `type-baseline.txt` (where present) may only shrink; coverage floors only rise.

## Docs
`docs/` (one page per feature, `docs/boundaries.md` for the service boundaries). Decisions and status live in the workspace `context/master-plan.md`.
