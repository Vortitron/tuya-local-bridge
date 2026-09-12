"""Repoint automations that target a device rather than an entity.

Moving an entity id carries every reference to it, because the id *is* the
reference. A device reference is not like that: Home Assistant's automation
editor writes ``device_id: <uuid>`` for its device triggers, conditions and
actions, and a uuid survives renaming, re-areaing and everything else. So a
converted device leaves those automations pointing at the cloud device the
swap just disabled — working automations that quietly do nothing.

Nothing about the device registry can fix that. The references have to be
rewritten, which means editing automations the user wrote. So every change is
previewed, counted, and recorded whole so it can be put back exactly.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class AutomationError(RuntimeError):
    """An automation could not be read or written."""


def count_references(config: Any, device_id: str) -> int:
    """How many times ``device_id`` appears as a device reference.

    Counts the value wherever it appears rather than only under a ``device_id``
    key: the editor also writes device ids into ``target`` blocks and into
    lists, and a reference missed is an automation left broken.
    """
    if not device_id:
        return 0
    if isinstance(config, str):
        return 1 if config == device_id else 0
    if isinstance(config, dict):
        return sum(count_references(v, device_id) for v in config.values())
    if isinstance(config, list):
        return sum(count_references(v, device_id) for v in config)
    return 0


def rewrite_references(config: Any, old_id: str, new_id: str) -> Any:
    """Return ``config`` with every ``old_id`` replaced by ``new_id``.

    Structure-preserving and non-mutating: the caller keeps the original to put
    back if anything downstream fails.
    """
    if not old_id or old_id == new_id:
        return config
    if isinstance(config, str):
        return new_id if config == old_id else config
    if isinstance(config, dict):
        return {k: rewrite_references(v, old_id, new_id) for k, v in config.items()}
    if isinstance(config, list):
        return [rewrite_references(v, old_id, new_id) for v in config]
    return config


def automation_ids(entities: Any) -> list[str]:
    """The ids the automation config API uses, from the entity registry.

    An automation's ``unique_id`` is the numeric id its config lives under, so
    the registry is the cheapest complete list of them.
    """
    found: list[str] = []
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        if entity.get("platform") != "automation":
            continue
        unique_id = entity.get("unique_id")
        if unique_id:
            found.append(str(unique_id))
    return found


class AutomationStore:
    """Read and write automation configurations over Home Assistant's REST API."""

    def __init__(self, base_url: str, token: str, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def get(self, automation_id: str) -> dict[str, Any] | None:
        response = requests.get(
            f"{self.base_url}/api/config/automation/config/{quote(str(automation_id))}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if response.status_code == 404:
            # An automation entity can outlive its stored config, e.g. one
            # defined in YAML rather than the editor. Skip rather than fail.
            return None
        if not response.ok:
            raise AutomationError(
                f"reading automation {automation_id}: {response.status_code}"
            )
        return response.json()

    def save(self, automation_id: str, config: dict[str, Any]) -> None:
        response = requests.post(
            f"{self.base_url}/api/config/automation/config/{quote(str(automation_id))}",
            headers=self._headers(),
            data=json.dumps(config),
            timeout=self.timeout,
        )
        if not response.ok:
            raise AutomationError(
                f"writing automation {automation_id}: {response.status_code} "
                f"{response.text[:200]}"
            )
