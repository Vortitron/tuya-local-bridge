"""A small web UI for reviewing and converting devices.

Deliberately server-rendered with no external assets: it is meant to run behind
Home Assistant ingress, where a strict CSP blocks CDN scripts and fonts, and to
work on a phone while you are stood next to the device you just power-cycled.

Conversion is two-phase because tuya-local asks which device type to use and
that choice is genuinely the user's — see :mod:`tuya_local_bridge.convert`.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from flask import Flask, redirect, render_template_string, request, url_for

from . import cloud as cloud_mod
from . import discovery as discovery_mod
from . import ha_discovery
from .convert import DirectFlowClient, VomeHomeFlowClient, convert
from .match import merge_lan, reconcile
from .store import ProvenanceStore

logger = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tuya Local Bridge</title>
<style>
  :root {
    --bg:#f6f7f9; --fg:#1c1e21; --muted:#6b7280; --card:#fff; --line:#e3e6ea;
    --accent:#0b7285; --ok:#0f7b3f; --warn:#9a6700; --err:#b42318;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#15181c; --fg:#e6e8eb; --muted:#9aa3ad; --card:#1d2126;
            --line:#2c3238; --accent:#3bb3c9; --ok:#4ec97f; --warn:#e0b341; --err:#f0796a; }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:1.25rem; background:var(--bg); color:var(--fg);
         font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  h1 { font-size:1.3rem; margin:0 0 .25rem; }
  h2 { font-size:1rem; margin:1.5rem 0 .5rem; }
  .sub { color:var(--muted); margin:0 0 1.25rem; }
  .counts { display:flex; flex-wrap:wrap; gap:.5rem; margin-bottom:1rem; }
  .pill { background:var(--card); border:1px solid var(--line); border-radius:999px;
          padding:.25rem .7rem; font-size:.85rem; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          overflow:hidden; margin-bottom:1rem; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:.55rem .7rem; border-bottom:1px solid var(--line);
          font-size:.9rem; }
  th { color:var(--muted); font-weight:600; font-size:.78rem; text-transform:uppercase;
       letter-spacing:.03em; }
  tr:last-child td { border-bottom:none; }
  code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em; }
  .wrap { overflow-x:auto; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px;
           padding:.6rem 1.1rem; font-size:.95rem; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  select { padding:.35rem; border-radius:6px; border:1px solid var(--line);
           background:var(--card); color:var(--fg); max-width:100%; }
  .muted { color:var(--muted); }
  .ok { color:var(--ok); } .warn { color:var(--warn); } .err { color:var(--err); }
  .note { background:var(--card); border:1px solid var(--line); border-left:3px solid var(--warn);
          border-radius:6px; padding:.7rem .9rem; margin-bottom:1rem; font-size:.9rem; }
  .qr { background:#fff; padding:1rem; border-radius:10px; display:inline-block; }
  form.inline { display:inline; }
</style></head><body>
<h1>Tuya Local Bridge</h1>
<p class="sub">{{ subtitle }}</p>
{{ body|safe }}
</body></html>"""


def create_app(
    state_dir: str,
    *,
    instance_id: str | None = None,
    vomehome_token: str | None = None,
    api_url: str = "https://vome.io",
    ha_url: str | None = None,
    ha_token: str | None = None,
    scan_seconds: int = 0,
    scan_cache_seconds: int = 300,
) -> Flask:
    """Build the app.

    Either give it VomeHome credentials (``instance_id`` + ``vomehome_token``)
    or direct Home Assistant ones (``ha_url`` + ``ha_token``).

    ``scan_seconds`` enables a UDP scan alongside Home Assistant's discovery.
    The two sources miss different devices, so merging them sees more than
    either alone — but a scan takes most of a minute, so the result is cached
    for ``scan_cache_seconds`` and refreshed on demand rather than per request.
    """
    app = Flask(__name__)
    app.config["STATE_DIR"] = state_dir

    session_path = os.path.join(state_dir, "session.json")
    store_path = os.path.join(state_dir, "provenance.json")

    def flow_client():
        if instance_id and vomehome_token:
            return VomeHomeFlowClient(instance_id, vomehome_token, api_url)
        if ha_url and ha_token:
            return DirectFlowClient(ha_url, ha_token)
        raise RuntimeError("no Home Assistant credentials configured")

    scan_cache: dict[str, Any] = {"at": 0.0, "devices": []}

    def lan_scan(force: bool = False):
        """Cached UDP scan; empty when scanning is disabled or unavailable."""
        if scan_seconds <= 0:
            return []
        fresh = (time.time() - scan_cache["at"]) < scan_cache_seconds
        if fresh and not force:
            return scan_cache["devices"]
        try:
            devices = discovery_mod.scan(scan_seconds)
        except discovery_mod.DiscoveryUnavailable:
            logger.warning("tinytuya not installed; skipping LAN scan")
            return []
        except OSError:
            # No host network, or broadcast blocked. HA's discovery still works.
            logger.warning("LAN scan failed; falling back to HA discovery", exc_info=True)
            return scan_cache["devices"]
        scan_cache.update(at=time.time(), devices=devices)
        return devices

    def discovery(force_scan: bool = False):
        """(devices, flow_id_by_device_id, converted_ids).

        Home Assistant's discovery goes first so its flow ids survive the merge;
        the scan then refines each record with the protocol version, which HA's
        flow does not carry.
        """
        if instance_id and vomehome_token:
            from_ha = ha_discovery.from_vomehome(instance_id, vomehome_token, api_url)
            converted = ha_discovery.converted_from_vomehome(
                instance_id, vomehome_token, api_url
            )
        else:
            from_ha = ha_discovery.from_home_assistant(ha_url or "", ha_token or "")
            try:
                converted = ha_discovery.converted_from_home_assistant(
                    ha_url or "", ha_token or ""
                )
            except Exception:
                logger.warning("could not read the device registry", exc_info=True)
                converted = set()

        devices = merge_lan(from_ha, lan_scan(force=force_scan))
        flows = {d.id: str(d.raw.get("flow_id") or "") for d in devices}
        return devices, flows, converted

    def page(body: str, subtitle: str = "", code: int = 200):
        return render_template_string(PAGE, body=body, subtitle=subtitle), code

    # ── login ──────────────────────────────────────────────────────────────

    @app.get("/login")
    def login_form():
        return page(
            '<div class="card" style="padding:1rem">'
            '<form method="post" action="/login">'
            "<p>Smart Life app &rarr; <b>Me</b> &rarr; gear &rarr; "
            "<b>Account and Security</b> &rarr; <b>User Code</b>.</p>"
            '<p><input name="user_code" placeholder="User Code" required '
            'style="padding:.5rem;border-radius:6px;border:1px solid var(--line)"> '
            "<button>Get QR code</button></p></form></div>",
            "Connect your Tuya account",
        )

    @app.post("/login")
    def login_start():
        user_code = (request.form.get("user_code") or "").strip()
        if not user_code:
            return redirect(url_for("login_form"))
        try:
            challenge = cloud_mod.request_qr(user_code)
        except cloud_mod.TuyaAuthError as exc:
            return page(f'<p class="err">{exc}</p><p><a href="/login">Try again</a></p>', "Login failed", 400)

        return page(
            f'<div class="card" style="padding:1rem;text-align:center">'
            f'<div class="qr">{_qr_svg(challenge.payload)}</div>'
            f"<p>Scan with Smart Life, then</p>"
            f'<form method="post" action="/login/finish">'
            f'<input type="hidden" name="user_code" value="{_esc(user_code)}">'
            f'<input type="hidden" name="token" value="{_esc(challenge.token)}">'
            f"<button>I have scanned it</button></form>"
            f'<p class="muted">The code expires after a couple of minutes.</p></div>',
            "Scan to connect",
        )

    @app.post("/login/finish")
    def login_finish():
        user_code = (request.form.get("user_code") or "").strip()
        token = (request.form.get("token") or "").strip()
        result = cloud_mod.poll_login(user_code, cloud_mod.QRChallenge(token=token))
        if result is None:
            return page(
                '<div class="note">Not scanned yet, or the code expired.</div>'
                '<p><a href="/login">Start again</a></p>',
                "Waiting for the scan",
                409,
            )
        cloud_mod.TuyaCloudSession(result, path=session_path).save()
        return redirect(url_for("index"))

    # ── status ─────────────────────────────────────────────────────────────

    @app.get("/")
    def index():
        if not os.path.exists(session_path):
            return redirect(url_for("login_form"))

        session = cloud_mod.TuyaCloudSession.load(session_path)
        cloud_devices = session.devices()
        lan_devices, flows, converted = discovery(
            force_scan=request.args.get("rescan") == "1"
        )

        store = ProvenanceStore(store_path)
        rotated = store.record_cloud(cloud_devices)
        store.record_lan(lan_devices)
        store.save()

        result = reconcile(cloud_devices, lan_devices, already_converted=converted)
        return page(
            _render_status(result, flows, rotated, scan_enabled=scan_seconds > 0),
            f"{session.username or 'connected'} — {len(cloud_devices)} devices on the account",
        )

    # ── conversion ─────────────────────────────────────────────────────────

    @app.post("/convert")
    def convert_start():
        """Phase one: submit keys, collect the type choices tuya-local wants."""
        chosen = set(request.form.getlist("device"))
        if not chosen:
            return redirect(url_for("index"))

        session = cloud_mod.TuyaCloudSession.load(session_path)
        lan_devices, flows, converted = discovery()
        result = reconcile(session.devices(), lan_devices, already_converted=converted)
        client = flow_client()

        pending, done, failed = [], [], []
        for matched in result.matched:
            if matched.id not in chosen:
                continue
            flow_id = flows.get(matched.id)
            if not flow_id:
                failed.append((matched, "no discovery flow — is tuya-local still running?"))
                continue
            try:
                outcome = convert(client, matched, flow_id)
            except Exception as exc:
                logger.exception("conversion failed for %s", matched.id)
                failed.append((matched, str(exc)))
                continue
            if outcome.status == "needs_type":
                pending.append((matched, flow_id, outcome))
            elif outcome.ok:
                done.append((matched, outcome))
            else:
                failed.append((matched, outcome.message or "; ".join(outcome.errors.values())))

        return page(_render_convert(pending, done, failed), "Choose a device type")

    @app.post("/convert/finish")
    def convert_finish():
        """Phase two: submit the chosen types."""
        session = cloud_mod.TuyaCloudSession.load(session_path)
        lan_devices, flows, converted = discovery()
        result = reconcile(session.devices(), lan_devices, already_converted=converted)
        client = flow_client()
        by_id = {m.id: m for m in result.matched}

        done, failed = [], []
        for key, device_type in request.form.items():
            if not key.startswith("type__") or not device_type:
                continue
            device_id = key[len("type__") :]
            matched = by_id.get(device_id)
            flow_id = request.form.get(f"flow__{device_id}") or flows.get(device_id)
            if matched is None or not flow_id:
                failed.append((device_id, "device is no longer pending"))
                continue
            try:
                outcome = convert(client, matched, flow_id, device_type=device_type)
            except Exception as exc:
                logger.exception("conversion failed for %s", device_id)
                failed.append((device_id, str(exc)))
                continue
            (done if outcome.ok else failed).append(
                (matched, outcome) if outcome.ok else (device_id, outcome.message)
            )

        rows = "".join(
            f'<tr><td>{_esc(m.cloud.name)}</td><td class="ok">converted</td>'
            f"<td><code>{_esc(o.title)}</code></td></tr>"
            for m, o in done
        ) + "".join(
            f'<tr><td><code>{_esc(str(d))}</code></td><td class="err">failed</td>'
            f"<td>{_esc(str(msg))}</td></tr>"
            for d, msg in failed
        )
        return page(
            f'<div class="card wrap"><table><tr><th>device</th><th>result</th><th></th></tr>'
            f"{rows}</table></div>"
            f'<p><a href="/">Back to status</a></p>',
            f"{len(done)} converted, {len(failed)} failed",
        )

    return app


# ── rendering helpers ──────────────────────────────────────────────────────


def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_status(
    result, flows: dict[str, str], rotated: list[str], *, scan_enabled: bool = False
) -> str:
    parts: list[str] = []

    counts = result.counts
    parts.append(
        '<div class="counts">'
        f'<span class="pill"><b>{counts["matched"]}</b> ready</span>'
        f'<span class="pill"><b>{counts["converted"]}</b> already local</span>'
        f'<span class="pill"><b>{counts["cloud_only"]}</b> not discovered</span>'
        f'<span class="pill"><b>{counts["lan_only"]}</b> unexplained</span>'
        "</div>"
    )

    if scan_enabled:
        parts.append(
            '<p><a href="/?rescan=1">Rescan the network</a> <span class="muted">(takes a moment; results are cached)</span></p>'
        )

    if rotated:
        parts.append(
            '<div class="note"><b>Local key changed</b> for '
            f"{_esc(', '.join(rotated))}. Any existing tuya-local entry for those "
            "devices is now dead and must be re-created.</div>"
        )

    if result.matched:
        rows = "".join(
            f'<tr><td><input type="checkbox" name="device" value="{_esc(m.id)}" '
            f'{"" if flows.get(m.id) else "disabled"}></td>'
            f"<td>{_esc(m.cloud.name)}</td>"
            f"<td><code>{_esc(m.lan.ip)}</code></td>"
            f"<td><code>{_esc(m.id)}</code></td>"
            f'<td class="muted">{_esc(m.cloud.category)}</td></tr>'
            for m in result.matched
        )
        parts.append(
            '<h2>Ready to convert</h2><form method="post" action="/convert">'
            f'<div class="card wrap"><table>'
            f"<tr><th></th><th>name</th><th>address</th><th>device id</th><th>type</th></tr>"
            f"{rows}</table></div>"
            "<button>Convert selected</button></form>"
        )

    if result.converted:
        parts.append(
            "<h2>Already on tuya-local</h2>"
            '<div class="card wrap"><table>'
            + "".join(
                f"<tr><td>{_esc(c.name)}</td><td><code>{_esc(c.id)}</code></td>"
                f'<td class="muted">{"online" if c.online else "offline"}</td></tr>'
                for c in result.converted
            )
            + "</table></div>"
        )

    if result.cloud_only:
        parts.append(
            "<h2>In your account, not discovered</h2>"
            '<p class="muted">Offline, on another subnet, or not local-capable.</p>'
            '<div class="card wrap"><table>'
            + "".join(
                f"<tr><td>{_esc(c.name)}</td><td><code>{_esc(c.id)}</code></td>"
                f'<td class="muted">{"online" if c.online else "offline"}</td></tr>'
                for c in result.cloud_only
            )
            + "</table></div>"
        )

    if result.lan_only:
        parts.append(
            "<h2>On the network, not in your account</h2>"
            '<p class="muted">Tuya hardware this account cannot see. Most often '
            "these are sold under another brand (LEDVANCE and many others) and "
            "paired in that brand&rsquo;s own app, which is a separate Tuya "
            "account; sometimes they have simply been reset. Either way no key "
            "is available here.</p>"
            '<div class="card wrap"><table>'
            + "".join(
                f"<tr><td><code>{_esc(d.ip)}</code></td><td><code>{_esc(d.id)}</code></td></tr>"
                for d in result.lan_only
            )
            + "</table></div>"
        )

    return "".join(parts)


def _render_convert(pending, done, failed) -> str:
    parts: list[str] = []

    if done:
        parts.append(
            '<div class="card wrap"><table><tr><th>converted straight away</th><th></th></tr>'
            + "".join(
                f"<tr><td>{_esc(m.cloud.name)}</td><td><code>{_esc(o.title)}</code></td></tr>"
                for m, o in done
            )
            + "</table></div>"
        )

    if pending:
        rows = "".join(
            f"<tr><td>{_esc(m.cloud.name)}</td><td>"
            f'<input type="hidden" name="flow__{_esc(m.id)}" value="{_esc(flow_id)}">'
            f'<select name="type__{_esc(m.id)}">'
            + "".join(f"<option>{_esc(opt)}</option>" for opt in outcome.type_options)
            + "</select></td></tr>"
            for m, flow_id, outcome in pending
        )
        parts.append(
            '<p>tuya-local could not tell what these are. Best guess is listed first.</p>'
            '<form method="post" action="/convert/finish">'
            f'<div class="card wrap"><table><tr><th>device</th><th>type</th></tr>{rows}</table></div>'
            "<button>Finish conversion</button></form>"
        )

    if failed:
        parts.append(
            '<h2>Failed</h2><div class="card wrap"><table>'
            + "".join(
                f"<tr><td>{_esc(m.cloud.name if hasattr(m, 'cloud') else m)}</td>"
                f'<td class="err">{_esc(msg)}</td></tr>'
                for m, msg in failed
            )
            + "</table></div>"
        )

    if not parts:
        parts.append('<div class="note">Nothing to do.</div>')

    parts.append('<p><a href="/">Back to status</a></p>')
    return "".join(parts)


def _qr_svg(payload: str, scale: int = 6) -> str:
    """Render the login QR as inline SVG.

    Inline rather than a data: URI so it survives the strictest ingress CSP.
    """
    try:
        import qrcode
    except ImportError:
        return f"<p>Install <code>qrcode</code> to display this.</p><p><code>{_esc(payload)}</code></p>"

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix) * scale

    squares = "".join(
        f'<rect x="{x * scale}" y="{y * scale}" width="{scale}" height="{scale}"/>'
        for y, row in enumerate(matrix)
        for x, cell in enumerate(row)
        if cell
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img" aria-label="Tuya login QR code">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<g fill="#000">{squares}</g></svg>'
    )
