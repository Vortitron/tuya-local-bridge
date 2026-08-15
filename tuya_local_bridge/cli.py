"""Command line interface.

    tuya-local-bridge login      # QR login, stores the session
    tuya-local-bridge cloud      # list cloud devices + keys
    tuya-local-bridge scan       # LAN discovery only
    tuya-local-bridge status     # the join: matched / cloud-only / LAN-only
    tuya-local-bridge export     # emit config for tuya-local or tinytuya
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Optional

from . import cloud as cloud_mod
from . import discovery as discovery_mod
from .match import reconcile
from .models import CloudDevice
from .store import ProvenanceStore

DEFAULT_DIR = os.environ.get(
    "TUYA_LOCAL_BRIDGE_DIR",
    os.path.join(os.path.expanduser("~"), ".config", "tuya-local-bridge"),
)
SESSION_FILE = "session.json"
STORE_FILE = "provenance.json"

# Keys older than this without cloud confirmation are suspect.
STALE_KEY_SECONDS = 30 * 24 * 3600


def _paths(args) -> tuple[str, str]:
    base = args.dir
    return os.path.join(base, SESSION_FILE), os.path.join(base, STORE_FILE)


def _load_session(args) -> cloud_mod.TuyaCloudSession:
    session_path, _ = _paths(args)
    if not os.path.exists(session_path):
        sys.exit(f"no session at {session_path} — run `tuya-local-bridge login` first")
    return cloud_mod.TuyaCloudSession.load(session_path)


def _mask(key: str, reveal: bool) -> str:
    if reveal or not key:
        return key or "-"
    return f"{key[:3]}…{key[-3:]}" if len(key) > 8 else "…"


def _render_qr(challenge: cloud_mod.QRChallenge) -> None:
    """Print the QR to the terminal, and save a PNG when possible."""
    print("\nScan with Smart Life / Tuya Smart  (Me -> scan icon, top right)\n")
    try:
        import qrcode  # noqa: PLC0415 - optional nicety

        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(challenge.payload)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print("(install `qrcode` to render this as a scannable code)")
    print(f"\npayload: {challenge.payload}")
    print("\nThis expires in a couple of minutes.\n")


# ── commands ───────────────────────────────────────────────────────────────


def cmd_login(args) -> int:
    session_path, _ = _paths(args)
    user_code = args.user_code or input(
        "User Code (Smart Life -> Me -> gear -> Account and Security): "
    ).strip()

    token = cloud_mod.login_interactive(user_code, _render_qr, timeout=args.timeout)
    session = cloud_mod.TuyaCloudSession(token, path=session_path)
    session.save()
    print(f"logged in as {session.username or '(unknown)'} — session saved to {session_path}")
    return 0


def cmd_cloud(args) -> int:
    session = _load_session(args)
    devices = session.devices()
    _, store_path = _paths(args)
    store = ProvenanceStore(store_path)
    rotated = store.record_cloud(devices)
    store.save()

    if args.json:
        print(json.dumps([d.raw for d in devices], indent=2))
        return 0

    print(f"{len(devices)} devices on the account\n")
    print(f"{'name':<28} {'device id':<24} {'local key':<20} {'category':<10} online")
    for d in sorted(devices, key=lambda x: x.name.lower()):
        print(
            f"{d.name[:27]:<28} {d.id:<24} {_mask(d.local_key, args.reveal):<20} "
            f"{d.category[:9]:<10} {'yes' if d.online else 'no'}"
        )

    if rotated:
        print(f"\n!! local key rotated for {len(rotated)} device(s): {', '.join(rotated)}")
        print("   any tuya-local entry using the old key is now dead — re-export.")
    return 0


def cmd_scan(args) -> int:
    devices = discovery_mod.scan(args.seconds, force_subnet_scan=args.force)
    _, store_path = _paths(args)
    store = ProvenanceStore(store_path)
    store.record_lan(devices)
    store.save()

    if args.json:
        print(json.dumps([d.raw for d in devices], indent=2))
        return 0

    print(f"{len(devices)} devices answered on the LAN\n")
    print(f"{'ip':<16} {'device id':<24} version")
    for d in devices:
        print(f"{d.ip:<16} {d.id:<24} {d.version or '?'}")
    return 0


def cmd_status(args) -> int:
    session = _load_session(args)
    _, store_path = _paths(args)
    store = ProvenanceStore(store_path)

    cloud_devices: list[CloudDevice] = session.devices()
    rotated = store.record_cloud(cloud_devices)

    lan_devices = [] if args.no_scan else discovery_mod.scan(args.seconds, force_subnet_scan=args.force)
    store.record_lan(lan_devices)
    store.save()

    result = reconcile(cloud_devices, lan_devices)

    print(
        f"\nmatched {len(result.matched)}  |  cloud-only {len(result.cloud_only)}"
        f"  |  lan-only {len(result.lan_only)}\n"
    )

    if result.matched:
        print("READY FOR TUYA-LOCAL")
        print(f"  {'name':<26} {'ip':<16} {'device id':<24} {'key':<16} ver")
        for m in result.matched:
            print(
                f"  {m.cloud.name[:25]:<26} {m.lan.ip:<16} {m.cloud.id:<24} "
                f"{_mask(m.cloud.local_key, args.reveal):<16} {m.lan.version or '?'}"
            )

    if result.cloud_only:
        print("\nIN CLOUD, NOT ON LAN  (offline, another subnet, or not local-capable)")
        for c in result.cloud_only:
            print(f"  {c.name[:25]:<26} {c.id:<24} {'online' if c.online else 'offline'}")

    if result.lan_only:
        print("\nON LAN, NOT IN CLOUD  (re-paired? another account? cached key is stale)")
        for d in result.lan_only:
            print(f"  {d.ip:<16} {d.id:<24} {d.version or '?'}")

    if rotated:
        print(f"\n!! key rotated for: {', '.join(rotated)}")

    stale = store.stale_keys(STALE_KEY_SECONDS)
    if stale:
        print(f"\n!! {len(stale)} migrated device(s) have unconfirmed keys — re-run to refresh")
    return 0


def cmd_export(args) -> int:
    session = _load_session(args)
    cloud_devices = session.devices()
    lan_devices = [] if args.no_scan else discovery_mod.scan(args.seconds, force_subnet_scan=args.force)
    result = reconcile(cloud_devices, lan_devices)

    if args.format == "tinytuya":
        payload = [
            {
                "name": m.cloud.name,
                "id": m.cloud.id,
                "key": m.cloud.local_key,
                "mac": m.lan.raw.get("mac", ""),
                "ip": m.lan.ip,
                "version": m.lan.version,
            }
            for m in result.matched
        ]
    else:  # tuya-local config-flow fields
        payload = [dict(m.config, name=m.cloud.name) for m in result.matched]

    print(json.dumps(payload, indent=2))
    return 0


# ── wiring ─────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tuya-local-bridge",
        description="Join Tuya cloud local_keys to LAN-discovered devices for tuya-local.",
    )
    p.add_argument("--dir", default=DEFAULT_DIR, help="state directory")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def add_scan_opts(sp):
        sp.add_argument("--seconds", type=int, default=discovery_mod.DEFAULT_SCAN_SECONDS)
        sp.add_argument("--force", action="store_true", help="also probe every subnet address")

    sp = sub.add_parser("login", help="QR login against the Smart Life app")
    sp.add_argument("user_code", nargs="?")
    sp.add_argument("--timeout", type=float, default=180.0)
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("cloud", help="list cloud devices and keys")
    sp.add_argument("--reveal", action="store_true", help="print keys unmasked")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_cloud)

    sp = sub.add_parser("scan", help="LAN discovery only")
    add_scan_opts(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("status", help="reconcile cloud against LAN")
    add_scan_opts(sp)
    sp.add_argument("--no-scan", action="store_true", help="skip discovery")
    sp.add_argument("--reveal", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("export", help="emit ready-to-use config for matched devices")
    add_scan_opts(sp)
    sp.add_argument("--no-scan", action="store_true")
    sp.add_argument("--format", choices=("tuya-local", "tinytuya"), default="tuya-local")
    sp.set_defaults(func=cmd_export)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    os.makedirs(args.dir, exist_ok=True)
    try:
        return args.func(args)
    except (cloud_mod.TuyaAuthError, discovery_mod.DiscoveryUnavailable) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
