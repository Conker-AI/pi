"""Web push mutants for the auth drill (scripts/auth_mutation_drill.py; docs/web-push.md).

Each case is (name, file, original text, mutated text, the test that must fail).
"""

PUSH = "tests/test_gateway_push.py::"
PUSH_CLAIM = "tests/test_push_queue.py::"
SESSION = "tests/test_gateway_push_session.py::"
PINNED = "tests/test_gateway_push_pinned.py::"
INSIDE = PINNED + "test_a_push_host_that_resolves_inside_the_network_is_never_connected"
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
        "if status in GONE:",
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
        'return [target for target in targets if target["id"] in on], _claimed(response)',
        "return targets, _claimed(response)",
        PUSH + "test_sign_out_everywhere_during_a_claim_sends_nothing",
    ),
    (
        "push-refused-device-kept",
        "gateway/push.py",
        '"DELETE FROM push_subscriptions WHERE id=? AND refused>=2"',
        '"DELETE FROM push_subscriptions WHERE id=? AND 0"',
        PUSH + "test_a_device_the_push_service_refuses_two_rounds_in_a_row_is_removed",
    ),
    (
        "push-refused-once-removed",
        "gateway/push.py",
        "AND refused>=2",
        "AND refused>=1",
        PUSH + "test_a_device_the_push_service_refuses_two_rounds_in_a_row_is_removed",
    ),
    (
        "push-renewal-turns-a-device-on",
        "gateway/push.py",
        "if renew and not known:",
        "if False:",
        PUSH + "test_a_renewal_never_turns_a_removed_device_back_on",
    ),
    (
        "push-device-outlives-its-sign-out",
        "gateway/store.py",
        'db.execute("DELETE FROM push_subscriptions WHERE session_id=?", (session_id,))',
        "pass",
        PUSH + "test_signing_out_the_browser_that_turned_a_device_on_turns_it_off",
    ),
    (
        "push-old-key-devices-kept",
        "gateway/push.py",
        '"DELETE FROM push_subscriptions WHERE key_id<>?"',
        '"DELETE FROM push_subscriptions WHERE key_id<>? AND 0"',
        PUSH + "test_a_new_server_key_drops_the_devices_turned_on_with_the_old_one",
    ),
    (
        "push-new-device-unannounced",
        "gateway/push_routes.py",
        "if new:\n            app.state.push.announce(device, label)",
        "if False:\n            app.state.push.announce(device, label)",
        PUSH + "test_the_other_devices_hear_when_one_more_is_turned_on",
    ),
    (
        "push-claim-reads-before-the-lock",
        "pi/agents/push_queue.py",
        'db.execute("BEGIN IMMEDIATE")',
        'db.execute("BEGIN")',
        PUSH_CLAIM + "test_a_chat_forgotten_while_the_inbox_is_read_is_never_claimed",
    ),
    (
        "push-client-not-pinned",
        "gateway/api.py",
        "transport=push_transport or PinnedTransport(),",
        "transport=push_transport,",
        INSIDE + "[inside0]",
    ),
    (
        "push-private-answer-connected",
        "gateway/push_transport.py",
        "if not addresses or not all(public(address) for address in addresses):",
        "if not addresses:",
        INSIDE + "[inside1]",
    ),
    (
        "push-mixed-answer-connected",
        "gateway/push_transport.py",
        "not all(public(address) for address in addresses)",
        "not any(public(address) for address in addresses)",
        INSIDE + "[inside19]",
    ),
    (
        "push-multicast-connected",
        "gateway/push_transport.py",
        "or each.is_multicast",
        "or False",
        INSIDE + "[inside7]",
    ),
    (
        "push-mapped-judged-as-ipv6-only",
        "gateway/push_transport.py",
        "for each in (ip, mapped)",
        "for each in (ip,)",
        PINNED + "test_a_mapped_address_is_judged_by_its_ipv4_address",
    ),
    (
        "push-device-left-on-an-idled-out-sign-in",
        "gateway/known_browsers.py",
        '"UPDATE push_subscriptions SET session_id=? WHERE session_id=?"',
        '"UPDATE push_subscriptions SET session_id=? WHERE session_id=? AND 0"',
        SESSION + "test_a_device_follows_its_browser_to_the_next_sign_in_without_a_renewal",
    ),
    (
        "push-old-table-sends-first",
        "gateway/push.py",
        (
            '"SELECT id,endpoint,p256dh,auth,created,session_id,key_id,label,refused"\n'
            '                " FROM push_subscriptions"'
        ),
        '"SELECT * FROM push_subscriptions"',
        SESSION + "test_a_gateway_database_from_before_refused_sends_nothing_and_keeps_running",
    ),
]
