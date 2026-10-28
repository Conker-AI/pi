"""After ToolGate records his decision on a request, Pi continues what waited on it.

Called from the browser's Inbox route and from `conker apply approvals`, only once ToolGate has
accepted the decision. Pi then runs the exact action (ToolGate checks the approval as it is spent)
or closes it and passes on his words. The decision stands whatever Pi answers: a Pi that is down
leaves the chat waiting, as before, and opening the chat still continues it.
"""

from __future__ import annotations

import httpx


def continue_after(client: httpx.Client, config, request_id: str, decision: dict) -> dict | None:
    if not config.pi_owner_key:
        return None
    body = {"status": decision.get("status"), "note": decision.get("note", "") or ""}
    try:
        response = client.post(
            config.pi_url.rstrip("/") + f"/approvals/{request_id}/decided",
            headers={"X-Pi-Owner-Key": config.pi_owner_key},
            json=body,
            timeout=15,
        )
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
