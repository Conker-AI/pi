"""Mutate isolated copies, requiring assertion failures rather than collection errors."""

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from auth_mutation_push import PUSH_CASES

ROOT = Path(__file__).resolve().parents[1]
API = "tests/test_gateway_api.py::"
STORE = "tests/test_gateway_store.py::"
VERIFY = "tests/test_gateway_verification.py::"
LOCKOUT = "tests/test_gateway_lockout.py::"
CHAT = "tests/test_gateway_chat_routes.py::"
LEVEL = "tests/test_gateway_approval_levels.py::"
CHAT_WRITE = CHAT + "test_owner_write_needs_only_session_csrf_and_an_exact_body"
CHAT_READ = CHAT + "test_owner_reads_through_the_control_key"
CAP = "tests/test_gateway_spending_cap.py::"
RECOVERY_CAP = "tests/test_spending_cap_password.py::"
OWNER_CHAT = (
    "tests/test_owner_chat_routes.py::test_owner_key_reaches_the_route_and_nothing_else_does"
)
CASES = [
    (
        "proof-replay-admitted",
        "gateway/store.py",
        'db.execute("DELETE FROM verification_proofs WHERE token_hash=?", (digest(proof),))',
        'db.execute("SELECT 1")',
        VERIFY + "test_concurrent_consumption_admits_exactly_once_and_stores_only_hashes",
    ),
    (
        "proof-body-binding-ignored",
        "gateway/store.py",
        'not hmac.compare_digest(found["fingerprint"], fingerprint)',
        "False",
        VERIFY + "test_canonical_binding_covers_full_body_identity_revision_and_route",
    ),
    (
        "tls-identity-changes-on-restart",
        "gateway/__main__.py",
        "        return certificate, key_path\n    key = rsa.generate_private_key",
        (
            "        certificate.unlink()\n        key_path.unlink()\n"
            "    key = rsa.generate_private_key"
        ),
        (
            "tests/test_gateway_tls.py::"
            "test_tls_identity_survives_restart_and_only_explicit_renewal_replaces_it"
        ),
    ),
    (
        "insecure-cookie",
        "gateway/api.py",
        "token,\n            secure=True,\n            httponly=True",
        "token,\n            secure=False,\n            httponly=True",
        API + "test_cookie_is_secure_httponly_strict_and_service_keys_do_not_authenticate",
    ),
    (
        "script-readable-cookie",
        "gateway/api.py",
        "token,\n            secure=True,\n            httponly=True",
        "token,\n            secure=True,\n            httponly=False",
        API + "test_cookie_is_secure_httponly_strict_and_service_keys_do_not_authenticate",
    ),
    (
        "cookie-namespace-ignored",
        "gateway/api.py",
        'return f"{COOKIE}-{self.cookie_namespace}" if self.cookie_namespace else COOKIE',
        "return COOKIE",
        "tests/test_gateway_cookie_namespace.py::test_two_ports_share_a_cookie_jar_without_replacing_sessions",
    ),
    (
        "csrf-disabled",
        "gateway/api.py",
        'if request.method not in {"GET", "HEAD", "OPTIONS"}:',
        "if False:",
        API + "test_unsafe_operations_require_csrf_and_logout_revokes_the_cookie",
    ),
    (
        "login-origin-unchecked",
        "gateway/api.py",
        'request.headers.get("origin") != app.state.config.origin',
        "False",
        API + "test_login_requires_csrf_and_exact_origin",
    ),
    (
        "anonymous-approval",
        "gateway/store.py",
        'if authenticated and not row["authenticated"]:',
        "if False:",
        API + "test_cookie_is_secure_httponly_strict_and_service_keys_do_not_authenticate",
    ),
    (
        "expired-session-survives",
        "gateway/store.py",
        (
            'if (\n            not row\n            or row["expires"] <= now\n'
            '            or (row["authenticated"] and (seen is None or seen + idle <= now))\n'
            "        ):"
        ),
        "if not row:",
        STORE + "test_idle_and_absolute_expiry",
    ),
    (
        "revocation-is-cosmetic",
        "gateway/store.py",
        'db.execute("DELETE FROM sessions WHERE id=?", (session_id,))',
        'db.execute("SELECT 1")',
        STORE + "test_login_rotates_and_logout_survives_restart",
    ),
    (
        "raw-session-token-at-rest",
        "gateway/store.py",
        "return hashlib.sha256(value.encode()).hexdigest()",
        "return value",
        STORE + "test_password_and_session_secrets_are_not_stored_verbatim",
    ),
    (
        "late-login-undoes-reset",
        "gateway/store.py",
        (
            "            self._session(db, token, authenticated=False)\n"
            "            # A reset or logout during scrypt must not be undone by a late login.\n"
            "            if not current or current[0] != generation:\n"
            '                raise AuthError("Credentials changed during login. '
            'Start sign-in again.")'
        ),
        "            pass",
        STORE + "test_password_reset_during_login_cannot_issue_a_stale_session",
    ),
    (
        "unlimited-password-attempts",
        "gateway/store.py",
        "blocked = attempts >= 30 or local >= 5",
        "blocked = False",
        STORE + "test_failed_logins_are_rate_limited_across_restarts",
    ),
    (
        "owner-key-reaches-worker",
        "gateway/service_routes.py",
        '"X-Pi-Gateway-Key",\n            app.state.config.pi_key,\n            body',
        '"X-Pi-Gateway-Key",\n            app.state.config.owner_key,\n            body',
        API + "test_proxy_keeps_credentials_separate_and_preserves_memory_status",
    ),
    (
        "approval-uses-agent-channel",
        "gateway/owner_routes.py",
        '/decision",\n            "X-ToolGate-Owner-Key",',
        '/decision",\n            "X-ToolGate-Execution-Key",',
        API + "test_proxy_keeps_credentials_separate_and_preserves_memory_status",
    ),
    (
        "chat-send-without-request-id",
        "gateway/service_routes.py",
        "if CHAT_SEND.fullmatch(target) and not (",
        "if False and not (",
        VERIFY + "test_a_chat_message_without_a_valid_request_id_never_reaches_pi",
    ),
    (
        "gateway-forwards-any-route",
        "gateway/service_routes.py",
        "if not runtime_allowed(request.method, target):",
        "if False:",
        API + "test_unknown_routes_and_forged_origins_never_reach_services",
    ),
    (
        "worker-runtime-key-becomes-admin",
        "pi/api/server.py",
        "if not runtime_allowed(request.method, request.url.path):",
        "if False:",
        API + "test_pi_runtime_key_has_no_owner_authority_or_future_admin_access",
    ),
    (
        "upstream-cookie-replayed",
        "gateway/api.py",
        '        outgoing.headers.pop("cookie", None)\n        return app.state.client.send(outgoing)',
        "        pass\n        return app.state.client.send(outgoing)",
        API + "test_proxy_keeps_credentials_separate_and_preserves_memory_status",
    ),
    (
        "upstream-redirect-accepted",
        "gateway/api.py",
        '        if 300 <= response.status_code < 400:\n            raise AuthError(\n                "Service returned an unexpected redirect. Check gateway service URLs on the host.",',
        '        if False:\n            raise AuthError(\n                "Service returned an unexpected redirect. Check gateway service URLs on the host.",',
        API + "test_upstream_failure_is_explicit_and_never_retried[redirect]",
    ),
    (
        "chat-body-unchecked",
        "gateway/chat_bodies.py",
        "BODIES[pattern].model_validate_json(json.dumps(body))",
        "None",
        CHAT_WRITE + "[sessions-output]",
    ),
    (
        "chat-read-query-unchecked",
        "gateway/chat_bodies.py",
        "_query(query, PAGED.get(pattern, set()))",
        "None",
        CHAT_READ + "[sessions-output]",
    ),
    (
        "chat-check-skipped",
        "gateway/service_routes.py",
        'check_chat_call(request.method, target, request.scope["query_string"], body)',
        "None",
        CHAT_WRITE + "[context-items-state]",
    ),
    (
        "chat-id-loosened",
        "pi/api/browser_contract.py",
        '_CHAT = r"ses_[0-9a-f]{16}"',
        '_CHAT = r"[A-Za-z0-9_-]+"',
        CHAT_READ + "[context-compaction]",
    ),
    (
        "owner-key-reaches-any-chat-path",
        "pi/api/server.py",
        "if owner_allowed(request.method, request.url.path) or owner_download_allowed(",
        "if True or owner_download_allowed(",
        OWNER_CHAT + "[POST-context-items-state]",
    ),
    (
        "own-budget-unlimited",
        "gateway/store.py",
        "blocked = own >= 5",
        "blocked = False",
        LOCKOUT + "test_a_known_browser_and_a_signed_in_tab_each_get_five_tries",
    ),
    (
        "unknown-device-trusted",
        "gateway/store.py",
        'if db.execute("SELECT 1 FROM known_devices WHERE token_hash=?", (hashed,)).fetchone():',
        "if True:",
        LOCKOUT + "test_a_browser_that_signed_in_before_still_signs_in_during_a_flood",
    ),
    (
        "confirmations-share-the-flood",
        "gateway/store.py",
        'return "session:" + row["id"]',
        "return None",
        LOCKOUT + "test_a_signed_in_tab_still_confirms_while_strangers_fill_the_shared_budget",
    ),
    (
        "wrong-password-not-reported",
        "gateway/store.py",
        '"INSERT INTO password_failures(at,origin) VALUES(?,?)", (self.clock(), origin)',
        '"SELECT 1"',
        LOCKOUT + "test_every_wrong_password_reaches_his_alerts_until_he_has_seen_it",
    ),
    (
        "raw-device-token-at-rest",
        "gateway/store.py",
        "known_browsers.signed_in(db, presented, digest(kept),",
        "known_browsers.signed_in(db, presented, kept,",
        LOCKOUT + "test_each_sign_in_replaces_the_device_token_and_only_its_hash_is_kept",
    ),
    (
        "sign-out-everywhere-keeps-browsers",
        "gateway/store.py",
        '                db.execute("DELETE FROM sessions")\n                db.execute("DELETE FROM known_devices")',
        '                db.execute("DELETE FROM sessions")',
        LOCKOUT
        + "test_signing_out_everywhere_or_a_new_password_forgets_every_browser[sign out everywhere]",
    ),
    (
        "script-readable-device-cookie",
        "gateway/auth_routes.py",
        'httponly=True,\n            samesite="strict",\n            path="/auth",',
        'httponly=False,\n            samesite="strict",\n            path="/auth",',
        LOCKOUT + "test_sign_in_gives_the_browser_a_lasting_device_cookie_for_auth_only",
    ),
    (
        "serious-yes-without-password",
        "gateway/owner_routes.py",
        'if approval_levels.needs_password(body["status"], level):',
        "if False:",
        LEVEL + "test_a_serious_or_unreadable_level_asks_for_the_password[view0]",
    ),
    (
        "missing-level-trusted-as-normal",
        "gateway/approval_levels.py",
        "return value if type(value) is int and value in LEVELS else SERIOUS",
        "return value if type(value) is int and value in LEVELS else NORMAL",
        LEVEL + "test_a_serious_or_unreadable_level_asks_for_the_password[view1]",
    ),
    (
        "another-requests-level-trusted",
        "gateway/approval_levels.py",
        ' or view.get("id") != identity:',
        ":",
        LEVEL + "test_a_serious_or_unreadable_level_asks_for_the_password[view8]",
    ),
    (
        "toolgate-down-trusted-as-normal",
        "gateway/owner_routes.py",
        "except (httpx.HTTPError, OSError, ValueError):\n            return approval_levels.SERIOUS",
        "except (httpx.HTTPError, OSError, ValueError):\n            return approval_levels.NORMAL",
        LEVEL + "test_a_serious_or_unreadable_level_asks_for_the_password[view13]",
    ),
    (
        "recovery-key-changes-the-cap",
        "pi/api/routes/owner_preferences.py",
        'keep = getattr(request.state, "owner_credential", False) is not True',
        "keep = False",
        RECOVERY_CAP + "test_the_recovery_key_cannot_change_the_spending_cap[change0]",
    ),
    (
        "host-cap-password-unchecked",
        "gateway/spending_cap.py",
        "auth.host_password(password)",
        "auth.configured()",
        CAP + "test_anything_else_is_refused_and_saves_nothing[payload0]",
    ),
    (
        "host-password-attempts-unlimited",
        "gateway/store.py",
        "WHERE budget='host' AND at>?",
        "WHERE budget='nobody' AND at>?",
        CAP + "test_wrong_passwords_are_limited_and_a_right_one_then_waits",
    ),
    (
        "host-password-reset-during-check-ignored",
        "gateway/store.py",
        'if not current or current[0] != owner["generation"]:',
        "if False:",
        CAP + "test_right_passwords_never_count_and_a_reset_during_the_check_stops_it",
    ),
]

# The host CLI asks for the password for what the browser asks it for: security preferences
# (`conker config set`, `gateway/host_preferences.py`) and a serious yes (`conker apply approvals`).
SECURITY = "tests/test_security_preferences_password.py::"
HOST_PREFERENCES = "tests/test_gateway_host_preferences.py::"
CLI_APPROVAL = "tests/test_gateway_cli_approval_password.py::"
CASES += [
    (
        f"recovery-key-changes-{field}",
        "pi/core/owner_preferences.py",
        'PASSWORD_FIELDS = ("idleTimeoutMinutes", "newChatAccess", "dailyBudget", "spendingCap")',
        f"PASSWORD_FIELDS = {kept}",
        SECURITY + f"test_the_recovery_key_cannot_change_a_security_preference[{selected}]",
    )
    for field, kept, selected in (
        (
            "sign-out-time",
            '("newChatAccess", "dailyBudget", "spendingCap")',
            "idleTimeoutMinutes-60",
        ),
        (
            "new-chat-access",
            '("idleTimeoutMinutes", "dailyBudget", "spendingCap")',
            "newChatAccess-full",
        ),
        (
            "daily-budgets",
            '("idleTimeoutMinutes", "newChatAccess", "spendingCap")',
            "dailyBudget.costCents-100000",
        ),
    )
] + [
    (
        "recovery-key-unstored-setting-judged-by-the-request",
        "pi/core/owner_preferences.py",
        "payload[key] != stored.get(key, DEFAULT[key])",
        "payload[key] != stored.get(key, payload[key])",
        SECURITY + "test_a_setting_stored_before_it_existed_reads_as_its_default",
    ),
    (
        "host-preferences-password-unchecked",
        "gateway/host_preferences.py",
        "auth.host_password(password)",
        "auth.configured()",
        HOST_PREFERENCES + "test_anything_else_is_refused_and_saves_nothing[payload0]",
    ),
    (
        "host-preferences-sign-out-time-not-enforced",
        "gateway/host_preferences.py",
        "auth.owner_window(",
        "(lambda *_args, **_options: None)(",
        HOST_PREFERENCES + "test_the_right_password_saves_with_the_owner_credential",
    ),
    (
        "host-late-save-loosens-the-sign-out-time",
        "gateway/host_preferences.py",
        "minutes, saved=False)",
        "minutes, saved=True)",
        HOST_PREFERENCES + "test_a_late_host_save_never_loosens_a_newer_sign_out_time",
    ),
    (
        "cli-serious-yes-without-password",
        "gateway/mutation.py",
        "        if serious:\n",
        "        if False:\n",
        CLI_APPROVAL
        + "test_a_serious_or_unreadable_yes_is_not_decided_without_the_password[view0]",
    ),
    (
        "cli-unreadable-level-trusted-as-normal",
        "gateway/mutation.py",
        "except (httpx.HTTPError, OSError, ValueError):\n        return approval_levels.SERIOUS",
        "except (httpx.HTTPError, OSError, ValueError):\n        return approval_levels.NORMAL",
        CLI_APPROVAL
        + "test_a_serious_or_unreadable_yes_is_not_decided_without_the_password[view13]",
    ),
    (
        "cli-approval-password-unchecked",
        "gateway/mutation.py",
        "auth.host_password(password)",
        "auth.configured()",
        CLI_APPROVAL + "test_a_password_sent_is_checked_even_when_the_level_needs_none",
    ),
]

# An imported MCP tool's read class: Observe (no Inbox card) needs the password.
MCP_CLASS = "tests/test_gateway_mcp_class.py::"
CASES += [
    (
        "mcp-observe-without-password",
        "gateway/mcp_class.py",
        'password=body["read_class"] != ACT_OUTWARD',
        "password=False",
        MCP_CLASS + "test_observe_without_the_password_is_428_and_never_reaches_toolgate",
    ),
    (
        "mcp-class-any-value",
        "gateway/mcp_class.py",
        'and body["read_class"] in (OBSERVE, ACT_OUTWARD)',
        "and True",
        MCP_CLASS + "test_anything_but_the_two_classes_is_refused_before_toolgate[body1]",
    ),
    (
        "mcp-class-echoes-toolgate-text",
        "gateway/mcp_class.py",
        "return plain(response) if response.status_code in (409, 422) else response",
        "return response",
        MCP_CLASS
        + "test_toolgate_refusals_come_back_with_a_plain_message[422-detail0-act_outward_name]",
    ),
]

# Web push (scripts/auth_mutation_push.py).
CASES += PUSH_CASES


def main() -> int:
    # In the OS temp directory, never in the checkout.
    run = Path(tempfile.mkdtemp(prefix="auth-mutations-"))
    baseline = ("baseline", None, None, None, None)
    for name, filename, old, new, selected in [baseline, *CASES]:
        target = run / name
        for directory in ("pi", "gateway"):
            shutil.copytree(
                ROOT / directory, target / directory, ignore=shutil.ignore_patterns("__pycache__")
            )
        (target / "tests").mkdir()
        for file in (
            "test_gateway_api.py",
            "test_gateway_store.py",
            "test_gateway_tls.py",
            "test_gateway_verification.py",
            "test_gateway_cookie_namespace.py",
            "test_gateway_chat_routes.py",
            "test_owner_chat_routes.py",
            "test_gateway_lockout.py",
            "test_gateway_approval_levels.py",
            "test_gateway_inbox_answers.py",
            "test_gateway_mcp.py",
            "test_gateway_spending_cap.py",
            "test_spending_cap_password.py",
            "test_security_preferences_password.py",
            "test_gateway_host_preferences.py",
            "test_gateway_cli_approval_password.py",
            "test_gateway_push.py",
            "test_gateway_push_session.py",
            "test_gateway_push_pinned.py",
            "test_push_queue.py",
            "test_gateway_mcp_class.py",
        ):
            shutil.copyfile(ROOT / "tests" / file, target / "tests" / file)
        if filename:
            file = target / filename
            source = file.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise RuntimeError(f"{name}: expected exactly one mutation site; update the drill")
            file.write_text(source.replace(old, new), encoding="utf-8")
        report = target / "results.xml"
        tests = (
            [selected] if selected else ["tests/test_gateway_api.py", "tests/test_gateway_store.py"]
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *tests,
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(target / "temp"),
                "--junitxml",
                str(report),
            ],
            cwd=target,
            env={**os.environ, "PYTHONPATH": str(target)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        (target / "output.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        suites = list(ET.parse(report).getroot().iter("testsuite")) if report.exists() else []
        failures = sum(int(s.get("failures", 0)) for s in suites)
        errors = sum(int(s.get("errors", 0)) for s in suites)
        skipped = sum(int(s.get("skipped", 0)) for s in suites)
        if (
            not suites
            or errors
            or skipped
            or (
                result.returncode != 0
                if name == "baseline"
                else result.returncode != 1 or failures < 1
            )
        ):
            print(f"FAILED {name}; inspect {target / 'output.txt'}")
            return 1
        print(f"{name}: {'passed' if name == 'baseline' else 'caught'}", flush=True)
    print(f"All {len(CASES)} mutants caught. Evidence: {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
