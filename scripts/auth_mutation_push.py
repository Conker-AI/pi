"""Web push mutants for the auth drill (scripts/auth_mutation_drill.py; docs/web-push.md).

Each case is (name, file, original text, mutated text, the test that must fail).
"""

PUSH = "tests/test_gateway_push.py::"
PUSH_CLAIM = "tests/test_push_queue.py::"
PUSH_CASES = [
    (
        "push-claim-relayed-for-the-browser",
        "gateway/service_routes.py",
        " or gateway_only(request.method, target):",
        ":",
        PUSH + "test_the_browser_cannot_make_a_claim_through_the_control_relay",
    ),
    (
        "push-to-any-address",
        "gateway/push.py",
        'and any(host == name or host.endswith("." + name) for name in PUSH_HOSTS)',
        "and True",
        PUSH + "test_a_stored_endpoint_outside_the_push_services_is_never_posted",
    ),
    (
        "push-gone-device-kept",
        "gateway/push.py",
        "if status in (404, 410):",
        "if False:",
        PUSH + "test_a_round_sends_fixed_words_to_every_device_and_drops_a_gone_one",
    ),
    (
        "push-devices-survive-sign-out-everywhere",
        "gateway/store.py",
        'db.execute("DELETE FROM known_devices")\n                db.execute("DELETE FROM push_subscriptions")',
        'db.execute("DELETE FROM known_devices")',
        PUSH + "test_sign_out_everywhere_and_a_new_password_turn_every_device_off",
    ),
    (
        "push-claimed-twice",
        "pi/agents/push_queue.py",
        '"SELECT 1 FROM push_deliveries WHERE item_key=?"',
        '"SELECT 1 FROM push_deliveries WHERE item_key=? AND 0"',
        PUSH_CLAIM + "test_an_item_is_claimed_once_and_the_claim_carries_no_words",
    ),
    (
        "push-ignores-quiet-hours",
        "pi/agents/push_queue.py",
        "if reasons:\n                held.update(reasons)",
        "if False:\n                held.update(reasons)",
        PUSH_CLAIM + "test_quiet_hours_hold_an_item_without_using_it_up",
    ),
    (
        "push-host-any-characters",
        "gateway/push.py",
        "and DNS_NAME.fullmatch(host) is not None",
        "and True",
        PUSH + "test_a_push_host_is_plain_dns_letters_only",
    ),
    (
        "push-devices-read-once",
        "gateway/push.py",
        'targets = [target for target in targets if target["id"] in on]',
        "targets = targets",
        PUSH + "test_sign_out_everywhere_during_a_claim_sends_nothing",
    ),
]
