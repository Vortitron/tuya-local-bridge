"""Minimal Home Assistant WebSocket client.

Only needed for the one thing REST cannot do: read the device registry, which
is how "already converted" is distinguished from "offline".  Everything else
this package needs is available over REST.

Inside an add-on the target is ``http://supervisor/core`` with
``SUPERVISOR_TOKEN``; the URL is rewritten to ``ws://.../api/websocket`` here.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class HaWebSocketError(RuntimeError):
    """The WebSocket API could not be reached, or refused the command."""


def websocket_url(base_url: str) -> str:
    """Turn an http(s) base URL into the HA WebSocket endpoint."""
    url = base_url.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://") :]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    elif not url.startswith(("ws://", "wss://")):
        url = "ws://" + url
    return f"{url}/api/websocket"


def command(
    base_url: str,
    token: str,
    payload: dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT,
) -> Any:
    """Run a single WebSocket command and return its result.

    Connects, authenticates, issues one command and closes — there is no
    long-lived subscription here, so a persistent connection would be a
    liability rather than a saving.
    """
    try:
        import websocket  # noqa: PLC0415 - optional, only for direct mode
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise HaWebSocketError(
            "websocket-client is required for direct Home Assistant access: "
            "pip install websocket-client"
        ) from exc

    url = websocket_url(base_url)
    connection = websocket.create_connection(url, timeout=timeout)
    try:
        greeting = json.loads(connection.recv())
        if greeting.get("type") != "auth_required":
            raise HaWebSocketError(f"unexpected greeting: {greeting.get('type')!r}")

        connection.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(connection.recv())
        if auth.get("type") != "auth_ok":
            raise HaWebSocketError(f"authentication failed: {auth.get('message') or auth}")

        connection.send(json.dumps({"id": 1, **payload}))
        # Skip anything that is not the reply to our command.
        while True:
            message = json.loads(connection.recv())
            if message.get("id") != 1 or message.get("type") != "result":
                continue
            if not message.get("success"):
                error = message.get("error") or {}
                raise HaWebSocketError(
                    f"{error.get('code', 'error')}: {error.get('message', message)}"
                )
            return message.get("result")
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing must not mask the real error
            logger.debug("error closing websocket", exc_info=True)
