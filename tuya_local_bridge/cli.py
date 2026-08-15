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
from . import ha_discovery
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


def _lan_devices(args):
    """Resolve the LAN half of the join from whichever source was chosen.

    ``ha`` reads Home Assistant's own tuya-local discovery flows, which works
    remotely; ``lan`` runs a UDP scan and must be on the broadcast domain.
    """
    if getattr(args, "no_scan", False):
        return []

    source = getattr(args, "source", "lan")
    if source == "ha":
        instance = args.instance or os.environ.get("VOMEHOME_INSTANCE_ID")
        token = args.token or os.environ.get("VOMEHOME_TOKEN")
        if not instance or not token:
            sys.exit(
                "--source ha needs --instance/--token "
                "(or VOMEHOME_INSTANCE_ID / VOMEHOME_TOKEN)"
            )
        return ha_discovery.from_vomehome(instance, token, args.api_url)
    if source == "ha-direct":
        url = args.ha_url or os.environ.get("HA_URL")
        token = args.token or os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
        if not url or not token:
            sys.exit("--source ha-direct needs --ha-url/--token (or HA_URL / HA_TOKEN)")
        return ha_discovery.from_home_assistant(url, token)
    return discovery_mod.scan(args.seconds, force_subnet_scan=args.force)


def _converted_ids(args) -> set[str]:
    """Device ids tuya-local already owns, when the source can tell us.

    Only the brokered path exposes the device registry today; a LAN scan cannot
    know, so it returns nothing and those devices simply stay in cloud-only.
    """
    if getattr(args, "source", "lan") != "ha":
        return set()
    instance = args.instance or os.environ.get("VOMEHOME_INSTANCE_ID")
    token = args.token or os.environ.get("VOMEHOME_TOKEN")
    if not instance or not token:
        return set()
    return ha_discovery.converted_from_vomehome(instance, token, args.api_url)


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
    devices = _lan_devices(args)
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
    if args.include_stored:
        # Keys learned from a vendor app account are not in this session.
        cloud_devices = cloud_devices + _stored_devices(
            args, {d.id for d in cloud_devices}
        )

    lan_devices = _lan_devices(args)
    store.record_lan(lan_devices)
    store.save()

    result = reconcile(cloud_devices, lan_devices, already_converted=_converted_ids(args))

    print(
        f"\nmatched {len(result.matched)}  |  already converted {len(result.converted)}"
        f"  |  cloud-only {len(result.cloud_only)}  |  lan-only {len(result.lan_only)}\n"
    )

    if result.matched:
        print("READY FOR TUYA-LOCAL")
        print(f"  {'name':<26} {'ip':<16} {'device id':<24} {'key':<16} ver")
        for m in result.matched:
            print(
                f"  {m.cloud.name[:25]:<26} {m.lan.ip:<16} {m.cloud.id:<24} "
                f"{_mask(m.cloud.local_key, args.reveal):<16} {m.lan.version or '?'}"
            )

    if result.converted:
        print("\nALREADY ON TUYA-LOCAL")
        for c in result.converted:
            print(f"  {c.name[:25]:<26} {c.id:<24} {'online' if c.online else 'offline'}")

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


def cmd_vendor(args) -> int:
    """Fetch local keys from a vendor-branded Tuya app account."""
    import getpass

    from .vendor import VENDORS, fetch_devices

    if args.vendor not in VENDORS:
        sys.exit(f"unknown vendor; known: {', '.join(sorted(VENDORS))}")

    email = args.email or input("Account email: ").strip()
    password = os.environ.get("TUYA_VENDOR_PASSWORD") or getpass.getpass("Password: ")

    devices = fetch_devices(
        args.vendor, email, password, region=args.region, country_code=args.country_code
    )

    _, store_path = _paths(args)
    store = ProvenanceStore(store_path)
    store.record_cloud(devices)
    store.save()

    print(f"\n{len(devices)} devices on the {VENDORS[args.vendor].label} account\n")
    print(f"{'name':<28} {'device id':<24} {'local key':<20} online")
    for d in sorted(devices, key=lambda x: x.name.lower()):
        print(
            f"{d.name[:27]:<28} {d.id:<24} {_mask(d.local_key, args.reveal):<20} "
            f"{'yes' if d.online else 'no'}"
        )
    print(f"\nRecorded in {store_path} — `status --include-stored` will use them.")
    return 0


def _stored_devices(args, known: set) -> list:
    """Devices whose keys we hold only from a previous vendor lookup."""
    _, store_path = _paths(args)
    store = ProvenanceStore(store_path)
    return [
        CloudDevice(
            id=record.device_id,
            name=record.name or record.device_id,
            local_key=record.local_key,
            product_id=record.product_id,
            category=record.category,
        )
        for record in store.devices.values()
        if record.local_key and record.device_id not in known
    ]


def _registry_and_devices(args):
    """(entity registry client, tuya_id -> {domain: ha device id})."""
    from . import ha_discovery as hd
    from .swap import DirectEntityRegistry, VomeHomeEntityRegistry

    instance = args.instance or os.environ.get("VOMEHOME_INSTANCE_ID")
    token = args.token or os.environ.get("VOMEHOME_TOKEN")
    if instance and token:
        return (
            VomeHomeEntityRegistry(instance, token, args.api_url),
            hd.map_devices(hd.device_registry_vomehome(instance, token, args.api_url)),
        )

    url = args.ha_url or os.environ.get("HA_URL")
    ha_token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    if url and ha_token:
        return (
            DirectEntityRegistry(url, ha_token),
            hd.map_devices(hd.device_registry_direct(url, ha_token)),
        )
    sys.exit("need --instance/--token, or --ha-url with HA_TOKEN")


def cmd_swap(args) -> int:
    """Move entity ids from the cloud device onto the local one."""
    from .swap import apply_swap, entities_for_device, plan_swap

    registry, devices = _registry_and_devices(args)
    _, store_path = _paths(args)
    store = ProvenanceStore(store_path)
    entities = registry.list_entities()

    convertible = sorted(
        d for d, m in devices.items() if "tuya" in m and "tuya_local" in m
    )
    targets = args.device_id or (convertible if args.all else [])
    if not targets:
        if not convertible:
            print("nothing converted yet — no device has both a cloud and a local entry")
            return 0
        # Defaulting to "everything" is dangerous: a device converted by hand
        # months ago may already have curated entity ids that a swap would undo.
        print("Devices with both a cloud and a local entry:\n")
        for tuya_id in convertible:
            print(f"  {tuya_id}")
        print(
            "\nName the ones to swap, or pass --all. Try --dry-run first: if you "
            "converted a device by hand and renamed its entities, swapping will "
            "replace those names with the cloud ids."
        )
        return 0

    changed = False
    for tuya_id in targets:
        mapping = devices.get(tuya_id) or {}
        if "tuya" not in mapping or "tuya_local" not in mapping:
            print(f"{tuya_id}: skipped (needs both a cloud and a local device)")
            continue

        plan = plan_swap(
            entities_for_device(entities, mapping["tuya"]),
            entities_for_device(entities, mapping["tuya_local"]),
        )
        print(f"\n{tuya_id}")
        for pair in plan.pairs:
            print(f"  {pair.local_entity_id}  ->  {pair.cloud_entity_id}")
        for entity_id in plan.cloud_unmatched:
            # Worth calling out: anything left here keeps pointing at the cloud,
            # so automations using it stay on the cloud path.
            print(f"  !! {entity_id}: no local counterpart — stays on the cloud")

        if plan.is_empty:
            continue
        if args.dry_run:
            continue

        for result in apply_swap(registry, plan, store, tuya_id):
            changed = True
            mark = "ok" if result.ok else "FAILED"
            print(f"  {mark}: {result.entity_id} ({result.detail})")

    if changed:
        store.save()
    elif args.dry_run:
        print("\n(dry run — nothing changed; drop --dry-run to apply)")
    return 0


def cmd_rollback(args) -> int:
    """Undo a swap, putting the cloud entity ids back."""
    from .swap import rollback

    registry, _ = _registry_and_devices(args)
    _, store_path = _paths(args)
    store = ProvenanceStore(store_path)

    targets = args.device_id or [
        d for d, r in store.devices.items() if r.active_migration is not None
    ]
    if not targets:
        print("nothing to roll back")
        return 0

    for tuya_id in targets:
        for result in rollback(registry, store, tuya_id):
            print(f"{'ok' if result.ok else 'FAILED'}: {result.entity_id} ({result.detail})")
    store.save()
    return 0


def cmd_serve(args) -> int:
    from .web import create_app  # imported lazily: Flask is an optional extra

    app = create_app(
        args.dir,
        instance_id=args.instance or os.environ.get("VOMEHOME_INSTANCE_ID"),
        vomehome_token=args.token or os.environ.get("VOMEHOME_TOKEN"),
        api_url=args.api_url,
        ha_url=args.ha_url or os.environ.get("HA_URL"),
        ha_token=os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN"),
        scan_seconds=args.scan_seconds,
    )
    print(f"listening on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port)
    return 0


def cmd_export(args) -> int:
    session = _load_session(args)
    cloud_devices = session.devices()
    lan_devices = _lan_devices(args)
    result = reconcile(cloud_devices, lan_devices, already_converted=_converted_ids(args))

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
        sp.add_argument(
            "--source",
            choices=("lan", "ha", "ha-direct"),
            default=os.environ.get("TUYA_LOCAL_BRIDGE_SOURCE", "lan"),
            help=(
                "where LAN facts come from: 'lan' scans locally; 'ha' reads Home "
                "Assistant's discovery via the VomeHome broker (works remotely); "
                "'ha-direct' reads it straight from Home Assistant"
            ),
        )
        sp.add_argument("--instance", help="VomeHome instance id (--source ha)")
        sp.add_argument("--token", help="VomeHome or Home Assistant token")
        sp.add_argument("--api-url", default="https://vome.io", help="VomeHome API base")
        sp.add_argument("--ha-url", help="Home Assistant base URL (--source ha-direct)")
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

    sp = sub.add_parser("scan", help="discovery only, no cloud call")
    add_scan_opts(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser(
        "vendor", help="local keys from a vendor app (LEDVANCE, SYLVANIA, ...)"
    )
    sp.add_argument("vendor", help="vendor key, e.g. ledvance")
    sp.add_argument("--email")
    sp.add_argument("--region", default="eu", choices=("eu", "us", "cn", "in"))
    sp.add_argument("--country-code", type=int, default=44)
    sp.add_argument("--reveal", action="store_true")
    sp.set_defaults(func=cmd_vendor)

    sp = sub.add_parser("status", help="reconcile cloud against LAN")
    add_scan_opts(sp)
    sp.add_argument(
        "--include-stored",
        action="store_true",
        help="also use keys recorded by a previous `vendor` lookup",
    )
    sp.add_argument("--no-scan", action="store_true", help="skip discovery")
    sp.add_argument("--reveal", action="store_true")
    sp.set_defaults(func=cmd_status)

    def add_ha_opts(sp):
        sp.add_argument("--instance", help="VomeHome instance id")
        sp.add_argument("--token", help="VomeHome token")
        sp.add_argument("--api-url", default="https://vome.io")
        sp.add_argument("--ha-url", help="Home Assistant base URL (direct mode)")

    sp = sub.add_parser(
        "swap", help="give the local entities the cloud entities' ids"
    )
    add_ha_opts(sp)
    sp.add_argument("device_id", nargs="*", help="Tuya device ids (default: all converted)")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sp.add_argument("--all", action="store_true", help="every converted device")
    sp.set_defaults(func=cmd_swap)

    sp = sub.add_parser("rollback", help="undo a swap")
    add_ha_opts(sp)
    sp.add_argument("device_id", nargs="*", help="Tuya device ids (default: all swapped)")
    sp.set_defaults(func=cmd_rollback)

    sp = sub.add_parser("serve", help="web UI for reviewing and converting devices")
    sp.add_argument("--host", default="0.0.0.0")  # noqa: S104 - add-on/ingress
    sp.add_argument("--port", type=int, default=8099)
    sp.add_argument("--instance", help="VomeHome instance id")
    sp.add_argument("--token", help="VomeHome token")
    sp.add_argument("--api-url", default="https://vome.io")
    sp.add_argument("--ha-url", help="Home Assistant base URL (direct mode)")
    sp.add_argument(
        "--scan-seconds",
        type=int,
        default=int(os.environ.get("TUYA_LOCAL_BRIDGE_SCAN_SECONDS", "0")),
        help="also scan the LAN and merge (0 disables; needs host network)",
    )
    sp.set_defaults(func=cmd_serve)

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
    except (
        cloud_mod.TuyaAuthError,
        discovery_mod.DiscoveryUnavailable,
        ha_discovery.HaDiscoveryError,
    ) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
