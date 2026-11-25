"""Host-CLI password mutants for the auth drill (scripts/auth_mutation_drill.py): lockdown off, tool
access grants and unasked publishing (`apply tools`), and `check-password`.

Each case is (name, file, original text, mutated text, the test that must fail).
"""

LOCKDOWN = "tests/test_gateway_lockdown_password.py::"
SETTINGS = "tests/test_gateway_tool_settings.py::"
CLI_TOOL = "tests/test_gateway_cli_tool_password.py::"
CHECK = "tests/test_gateway_check_password.py::"
PUBLISH_RULE = 'password = value.authorization == "auto" or value.conkerMayUse'
HOST_CASES = [
    (
        "settings-lockdown-off-without-the-proof",
        "gateway/lockdown.py",
        "PASSWORD_PROOF if checked else None,",
        "None,",
        LOCKDOWN + "test_the_settings_switch_sends_the_proof_only_after_the_password",
    ),
    (
        "settings-lockdown-proof-without-the-password",
        "gateway/lockdown.py",
        "                admit_write(request, body)\n                checked = True",
        "                checked = True",
        SETTINGS + "test_lockdown_turns_on_with_the_session_and_off_only_with_the_password",
    ),
    (
        "host-lockdown-password-unchecked",
        "gateway/lockdown.py",
        "auth.host_password(password)",
        "auth.configured()",
        LOCKDOWN + "test_anything_else_changes_nothing_and_is_not_repeated[payload0]",
    ),
    (
        "host-lockdown-without-the-proof",
        "gateway/lockdown.py",
        "                **PASSWORD_PROOF,\n",
        "",
        LOCKDOWN + "test_the_right_password_lifts_it_with_the_proof",
    ),
    (
        "cli-tool-grant-without-password",
        "gateway/mutation.py",
        "password = isinstance(value, ToolAccess) and value.enabled",
        "password = False",
        CLI_TOOL + "test_a_grant_or_a_publish_that_runs_unasked_is_not_sent_without_the_password"
        "[access]",
    ),
    (
        "cli-auto-publish-without-password",
        "gateway/mutation.py",
        PUBLISH_RULE,
        "password = value.conkerMayUse",
        CLI_TOOL + "test_a_grant_or_a_publish_that_runs_unasked_is_not_sent_without_the_password"
        "[auto]",
    ),
    (
        "cli-conker-may-use-without-password",
        "gateway/mutation.py",
        PUBLISH_RULE,
        'password = value.authorization == "auto"',
        CLI_TOOL + "test_a_grant_or_a_publish_that_runs_unasked_is_not_sent_without_the_password"
        "[conker]",
    ),
    (
        "cli-tool-password-sent-unchecked",
        "gateway/mutation.py",
        "    elif password is not None:\n",
        "    elif False:\n",
        CLI_TOOL + "test_a_wrong_or_malformed_password_sends_nothing_and_is_not_repeated[wrong]",
    ),
    (
        "cli-tool-password-never-checked-by-the-command",
        "gateway/__main__.py",
        "auth = AuthStore(args.db) if args.resource in PASSWORD_RESOURCES else None",
        'auth = AuthStore(args.db) if args.resource == "approvals" else None',
        CLI_TOOL + "test_the_command_checks_tool_changes_and_exits_3_without_the_password",
    ),
    (
        "check-password-unchecked",
        "gateway/host_check.py",
        "auth.host_password(password)",
        "auth.configured()",
        CHECK + "test_anything_else_fails_without_repeating_it[payload0]",
    ),
]
