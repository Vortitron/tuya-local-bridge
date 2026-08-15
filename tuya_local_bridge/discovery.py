"""Find Tuya devices broadcasting on the local network.

Devices announce themselves over UDP on 6666 (plaintext, protocol 3.1), 6667
(AES-ECB, 3.3/3.4) and 7000 (AES-GCM, 3.5).  Parsing all three correctly is
fiddly and already solved, so this module wraps :mod:`tinytuya`'s scanner
rather than reimplementing the wire format.

Discovery only works from *inside* the broadcast domain — it must run on the
Home Assistant host, not on a remote server.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .models import LanDevice

logger = logging.getLogger(__name__)

DEFAULT_SCAN_SECONDS = 18


class DiscoveryUnavailable(RuntimeError):
    """tinytuya is not installed."""


def scan(
    seconds: int = DEFAULT_SCAN_SECONDS,
    *,
    force_subnet_scan: bool = False,
) -> list[LanDevice]:
    """Listen for device broadcasts and return what was heard.

    ``seconds`` matters: devices re-announce roughly every 5s, so a short scan
    silently under-reports.  The default listens long enough to catch a few
    rounds.

    ``force_subnet_scan`` additionally probes every address on the local
    subnet, which finds devices whose broadcasts do not reach us (common with
    VLANs or AP isolation) at the cost of being much slower and noisier.
    """
    try:
        import tinytuya  # noqa: PLC0415 - optional, and only needed on the LAN
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DiscoveryUnavailable(
            "tinytuya is required for LAN discovery: pip install tinytuya"
        ) from exc

    logger.info("scanning the LAN for %ss", seconds)
    found: dict[str, Any] = tinytuya.deviceScan(
        verbose=False,
        maxretry=seconds,
        # Polling needs local keys, which is precisely what we do not have yet.
        poll=False,
        byID=False,
        forcescan=force_subnet_scan,
    )
    return _normalise(found)


def _normalise(found: dict[str, Any]) -> list[LanDevice]:
    """Turn tinytuya's ip-keyed dict into :class:`LanDevice` values."""
    devices: list[LanDevice] = []
    for key, raw in (found or {}).items():
        if not isinstance(raw, dict):
            continue
        device_id = raw.get("gwId") or raw.get("id") or ""
        ip = raw.get("ip") or (key if _looks_like_ip(key) else "")
        if not device_id or not ip:
            logger.debug("skipping incomplete discovery record: %r", raw)
            continue
        devices.append(
            LanDevice(
                id=str(device_id),
                ip=str(ip),
                version=_version_str(raw.get("version")),
                product_key=str(raw.get("productKey") or ""),
                raw=raw,
            )
        )
    devices.sort(key=lambda d: _ip_sort_key(d.ip))
    return devices


def _version_str(version: Optional[Any]) -> str:
    if version in (None, ""):
        return ""
    return str(version)


def _looks_like_ip(value: str) -> bool:
    parts = str(value).split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def _ip_sort_key(ip: str) -> tuple:
    if _looks_like_ip(ip):
        return tuple(int(p) for p in ip.split("."))
    return (999, 999, 999, 999)
