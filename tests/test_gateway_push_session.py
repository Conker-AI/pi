"""A device is turned on only for a sign-in that still exists (gateway/push.py Devices.add)."""

import pytest
from test_gateway_push import FCM, device, world  # noqa: F401  (world is a fixture)

from gateway import push
from gateway import store as auth_module


def test_a_device_is_never_put_on_a_sign_in_that_was_signed_out(world):
    _, _, _, app, _ = world
    _, p256dh, secret = device()
    gone = app.state.auth.sessions()[0]["id"]
    app.state.auth.revoke(gone)  # sign-out lands while the request waits for its body
    for renew in (False, True):
        with pytest.raises(auth_module.AuthError) as refused:
            push.Devices(app.state.auth).add(
                FCM, p256dh, secret, session=gone, key="k", label="Chrome", renew=renew
            )
        assert refused.value.status == 401
    assert push.Devices(app.state.auth).ids() == []
