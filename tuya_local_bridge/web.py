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
import threading
import time
from typing import Any

from flask import Flask, redirect, render_template_string, request, url_for

from . import __version__, ha_discovery
from . import cloud as cloud_mod
from . import discovery as discovery_mod
from .convert import (
    TUYA_LOCAL_PORT,
    DirectFlowClient,
    VomeHomeFlowClient,
    convert,
    reachable,
)
from .match import drop_shared_addresses, merge_lan, reconcile
from .store import ProvenanceStore
from .swap import (
    DirectEntityRegistry,
    VomeHomeEntityRegistry,
    add_manual_pairs,
    apply_swap,
    candidates_for,
    entities_for_device,
    plan_swap,
)

logger = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{% if refresh_seconds %}<meta http-equiv="refresh" content="{{ refresh_seconds }}">{% endif %}
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
  .version { color:var(--muted); font-size:.78rem; margin-top:2rem;
             border-top:1px solid var(--line); padding-top:.6rem; }
  .scanning { display:flex; align-items:center; gap:.7rem; }
  .working { display:none; align-items:center; gap:.7rem; margin-top:1rem; }
  .working.on { display:flex; }
  .spinner { width:1.1rem; height:1.1rem; border:2px solid var(--line);
             border-top-color:var(--fg); border-radius:50%;
             animation:spin 0.9s linear infinite; flex:none; }
  @keyframes spin { to { transform:rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spinner { animation-duration:3s; } }
</style></head><body>
<h1>Tuya Local Bridge</h1>
<p class="sub">{{ subtitle }}</p>
{{ body|safe }}
<p class="version">Tuya Local Bridge {{ version }}</p>
<script>
/* Converting talks to Home Assistant and, when a device has moved, scans the
 * whole subnet looking for it. That can run past a minute with nothing on
 * screen, which is indistinguishable from a page that has died -- so say what
 * is happening and keep counting, rather than leaving a still button and a
 * blank wait.
 *
 * Progressive enhancement only: without this the form still submits. */
(function () {
  var forms = document.querySelectorAll('form[data-working]');
  for (var i = 0; i < forms.length; i++) {
    wire(forms[i]);
  }

  function wire(form) {
    form.addEventListener('submit', function () {
      var button = form.querySelector('button');
      if (button) { button.disabled = true; }

      var box = document.createElement('div');
      box.className = 'working on';
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      box.innerHTML = '<div class="spinner"></div>';

      var text = document.createElement('div');
      box.appendChild(text);
      form.appendChild(box);

      var note = form.getAttribute('data-working');
      var slow = form.hasAttribute('data-slow');
      var started = Date.now();

      function tick() {
        var seconds = Math.round((Date.now() - started) / 1000);
        text.textContent = note + ' ' + seconds + 's.' + (
          slow && seconds > 25
            ? ' Still going \u2014 a device that has moved is being looked for,'
              + ' which takes a minute or two. Leave this page open.'
            : ''
        );
      }

      tick();
      setInterval(tick, 1000);
    });
  }
})();
</script>
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
    heal_interval_hours: float = 0,
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

    # Home Assistant ingress serves the add-on under a generated prefix such
    # as /api/hassio_ingress/<token>/ and passes it in X-Ingress-Path. An app
    # that ignores it emits absolute URLs like /login, which escape the prefix
    # and land on Home Assistant's own root — a 404, with nothing to suggest
    # the add-on is the thing at fault.
    #
    # Setting SCRIPT_NAME is the standard fix: url_for then generates prefixed
    # URLs everywhere, without every handler having to know it is behind a
    # proxy. The header is only present via ingress, so direct access is
    # unaffected.
    class _IngressPrefix:
        def __init__(self, wsgi_app):
            self.wsgi_app = wsgi_app

        def __call__(self, environ, start_response):
            prefix = environ.get("HTTP_X_INGRESS_PATH", "")
            if prefix:
                environ["SCRIPT_NAME"] = prefix.rstrip("/")
                path = environ.get("PATH_INFO", "")
                if path.startswith(prefix):
                    environ["PATH_INFO"] = path[len(prefix):] or "/"
            return self.wsgi_app(environ, start_response)

    app.wsgi_app = _IngressPrefix(app.wsgi_app)

    session_path = os.path.join(state_dir, "session.json")
    store_path = os.path.join(state_dir, "provenance.json")

    def use_broker() -> bool:
        """Whether to go via the VomeHome broker rather than straight to HA.

        A direct connection wins whenever one is configured — that is what a
        plain install and the add-on both have. The broker is for reaching an
        instance this process cannot route to itself.
        """
        return not (ha_url and ha_token) and bool(instance_id and vomehome_token)

    def flow_client():
        if use_broker():
            return VomeHomeFlowClient(instance_id, vomehome_token, api_url)
        if ha_url and ha_token:
            return DirectFlowClient(ha_url, ha_token)
        raise RuntimeError("no Home Assistant credentials configured")

    def entity_registry():
        if use_broker():
            return VomeHomeEntityRegistry(instance_id, vomehome_token, api_url)
        return DirectEntityRegistry(ha_url, ha_token)

    def device_map():
        """Tuya device id -> {domain: Home Assistant device id}.

        A converted device exists twice, once from the cloud integration and
        once from tuya-local. That pairing is what makes the swap possible.
        """
        if use_broker():
            raw = ha_discovery.device_registry_vomehome(
                instance_id, vomehome_token, api_url
            )
            owners = ha_discovery.config_entry_domains_vomehome(
                instance_id, vomehome_token, api_url
            )
        else:
            raw = ha_discovery.device_registry_direct(ha_url or "", ha_token or "")
            owners = ha_discovery.config_entry_domains_direct(ha_url or "", ha_token or "")
        return ha_discovery.map_devices(raw, entry_domains=owners)

    def swap_plans(device_ids):
        """(plans, problems) for the devices asked about."""
        registry = entity_registry()
        devices = device_map()
        entities = registry.list_entities()

        plans, problems = [], []
        for tuya_id in device_ids:
            mapping = devices.get(tuya_id) or {}
            if "tuya" not in mapping or "tuya_local" not in mapping:
                problems.append(
                    (tuya_id, "needs both a cloud and a local device in Home Assistant")
                )
                continue
            plan = plan_swap(
                entities_for_device(entities, mapping["tuya"]),
                entities_for_device(entities, mapping["tuya_local"]),
            )
            if plan.is_empty and not plan.cloud_unmatched:
                problems.append((tuya_id, "nothing pairs up to swap"))
                continue
            plans.append((tuya_id, plan))
        return registry, plans, problems

    scan_lock = threading.Lock()
    scan_cache: dict[str, Any] = {
        "at": 0.0,
        "devices": [],
        "running": False,
        "started": 0.0,
        "error": None,
    }

    def lan_scan(force: bool = False, deep: bool = False):
        """Whatever the last scan found, starting a new one if it is due.

        A scan takes most of a minute, and doing it inline meant the first page
        load simply did not answer until it finished — indistinguishable from a
        hung request, with the browser's own spinner the only sign of life. So
        it runs on a thread and the page renders straight away, saying that a
        scan is in progress rather than pretending there is nothing to find.
        """
        if scan_seconds <= 0:
            return []
        fresh = (time.time() - scan_cache["at"]) < scan_cache_seconds
        if force or deep or (not fresh and not scan_cache["running"]):
            _start_scan(deep=deep)
        return scan_cache["devices"]

    def _start_scan(deep: bool = False) -> bool:
        """Kick off a scan unless one is already running. True if it started."""
        with scan_lock:
            if scan_cache["running"]:
                return False
            scan_cache.update(running=True, started=time.time(), error=None)

        def run() -> None:
            try:
                devices = discovery_mod.scan(scan_seconds, force_subnet_scan=deep)
                with scan_lock:
                    scan_cache.update(devices=devices, at=time.time())
            except discovery_mod.DiscoveryUnavailable:
                logger.warning("tinytuya not installed; skipping LAN scan")
                with scan_lock:
                    scan_cache["error"] = "tinytuya is not installed"
            except OSError:
                # No host network, or broadcast blocked. HA's discovery still works.
                logger.warning("LAN scan failed; keeping the last result", exc_info=True)
                with scan_lock:
                    scan_cache["error"] = "the network could not be scanned"
            except Exception as exc:
                logger.exception("LAN scan failed")
                with scan_lock:
                    scan_cache["error"] = str(exc)
            finally:
                with scan_lock:
                    scan_cache["running"] = False

        threading.Thread(target=run, name="tuya-lan-scan", daemon=True).start()
        return True

    def discovery(force_scan: bool = False, deep: bool = False):
        """(devices, flow_id_by_device_id, converted_ids).

        Home Assistant's discovery goes first so its flow ids survive the merge;
        the scan then refines each record with the protocol version, which HA's
        flow does not carry.
        """
        if use_broker():
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

        # Home Assistant's discovery can hold the address of whatever forwarded
        # the broadcast rather than the device's own, so strip any address
        # several devices claim before the scan gets a chance to correct it.
        devices = merge_lan(
            drop_shared_addresses(from_ha), lan_scan(force=force_scan, deep=deep)
        )
        flows = {d.id: str(d.raw.get("flow_id") or "") for d in devices}
        return devices, flows, converted

    def page(body: str, subtitle: str = "", code: int = 200, refresh_seconds: int = 0):
        return (
            render_template_string(
                PAGE,
                body=body,
                subtitle=subtitle,
                refresh_seconds=refresh_seconds,
                version=__version__,
            ),
            code,
        )

    # ── login ──────────────────────────────────────────────────────────────

    @app.get("/login")
    def login_form():
        return page(
            '<div class="card" style="padding:1rem">'
            '<form method="post" action="' + url_for("login_start") + '">'
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
            return page(f'<p class="err">{exc}</p><p><a href="{url_for("login_form")}">Try again</a></p>', "Login failed", 400)

        return page(
            f'<div class="card" style="padding:1rem;text-align:center">'
            f'<div class="qr">{_qr_svg(challenge.payload)}</div>'
            f"<p>Scan with Smart Life, then</p>"
            f'<form method="post" action="{url_for("login_finish")}">'
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
                '<p><a href="' + url_for("login_form") + '">Start again</a></p>',
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
        with scan_lock:
            scanning = bool(scan_cache["running"])
            scan_error = scan_cache["error"]
            scan_started = scan_cache["started"]
        return page(
            _render_status(
                result,
                flows,
                rotated,
                scan_enabled=scan_seconds > 0,
                swapped=frozenset(
                    device_id
                    for device_id, record in store.devices.items()
                    if record.active_migration is not None
                ),
                scanning=scanning,
                scan_error=scan_error,
                scan_elapsed=int(time.time() - scan_started) if scanning else 0,
            ),
            f"{session.username or 'connected'} — {len(cloud_devices)} devices on the account",
            # While a scan runs the page is incomplete, so bring the answer to
            # the reader rather than making them guess when to press reload.
            refresh_seconds=6 if scanning else 0,
        )

    # ── conversion ─────────────────────────────────────────────────────────

    @app.post("/convert/confirm")
    def convert_confirm():
        """Say what is about to happen before anything happens.

        Conversion writes to Home Assistant, and the run is not all-or-nothing
        -- most devices need a type chosen part way through. Someone pressing
        a button labelled "Convert selected" deserves to know both of those
        before they press it, not after.
        """
        chosen = set(request.form.getlist("device"))
        if not chosen:
            return redirect(url_for("index"))

        session = cloud_mod.TuyaCloudSession.load(session_path)
        lan_devices, _flows, converted = discovery()
        result = reconcile(session.devices(), lan_devices, already_converted=converted)
        picked = [m for m in result.matched if m.id in chosen]

        return page(_render_confirm(picked), "Before we start")

    @app.post("/convert")
    def convert_start():
        """Phase one: submit keys, collect the type choices tuya-local wants."""
        chosen = set(request.form.getlist("device"))
        if not chosen:
            return redirect(url_for("index"))

        session = cloud_mod.TuyaCloudSession.load(session_path)
        lan_devices, flows, converted = discovery()
        result = reconcile(session.devices(), lan_devices, already_converted=converted)

        # Addresses go stale.  Home Assistant's discovery remembers every
        # device it has ever heard and never expires the address, and a plain
        # UDP scan only refreshes the ones still shouting -- over a bridged
        # tunnel, several are not.  Handing tuya-local a dead address gets
        # "base: connection" back, which is true and useless.  So if anything
        # the user picked does not answer, go and find it properly first.
        if any(
            not reachable(m.lan.ip)
            for m in result.matched
            if m.id in chosen
        ):
            logger.info("a selected device did not answer; running a deep scan")
            lan_devices, flows, converted = discovery(force_scan=True, deep=True)
            result = reconcile(
                session.devices(), lan_devices, already_converted=converted
            )

        client = flow_client()

        pending, done, failed = [], [], []
        for matched in result.matched:
            if matched.id not in chosen:
                continue
            flow_id = flows.get(matched.id)
            if not flow_id:
                failed.append((matched, "no discovery flow — is tuya-local still running?"))
                continue

            # Check the device actually answers before handing the address to
            # tuya-local.  Addresses come from a LAN scan and devices move, so
            # by the time someone ticks the box the address can be dead.
            # tuya-local reports that as "base: connection", which is accurate
            # and tells the owner nothing they can do about it.
            if not reachable(matched.lan.ip):
                failed.append((
                    matched,
                    f"no answer from {matched.lan.ip} on port {TUYA_LOCAL_PORT} — "
                    "the device is switched off, or it has moved to a different "
                    "address since it was found. Rescan and try again.",
                ))
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
        offer = ""
        if done:
            # Converting mints new entities; everything that referred to the
            # cloud ones is now pointing at a device nobody updates. Saying so
            # here is the difference between a finished job and half of one.
            offer = (
                '<div class="note"><b>Your automations do not know about these '
                "yet.</b> Converting created new entities, so automations, "
                "scripts and dashboards still point at the old cloud ones and "
                "will quietly stop working. Moving the ids across fixes that "
                "without editing any of them.</div>"
                '<form method="post" action="'
                + _u("swap_confirm")
                + '">'
                + "".join(
                    f'<input type="hidden" name="device" value="{_esc(m.id)}">'
                    for m, _o in done
                )
                + "<button>Move the entity ids across</button></form>"
            )

        return page(
            f'<div class="card wrap"><table><tr><th>device</th><th>result</th><th></th></tr>'
            f"{rows}</table></div>"
            f"{offer}"
            f'<p><a href="{_u("index")}">Back to status</a></p>',
            f"{len(done)} converted, {len(failed)} failed",
        )

    @app.post("/swap/confirm")
    def swap_confirm():
        """Show exactly which id moves where, before anything moves.

        Converting mints new entities, so automations, scripts and dashboards
        still point at the cloud ones and quietly stop working. The repair is
        to give the local entity the id the cloud one had -- then nothing that
        references it needs changing at all.

        It is previewed rather than just done because it is not always wanted:
        a device converted by hand may already have a better id than the cloud
        ever gave it, and swapping would throw that away.
        """
        chosen = request.form.getlist("device")
        if not chosen:
            return redirect(_u("index"))

        _registry, plans, problems = swap_plans(chosen)
        return page(_render_swap_preview(plans, problems), "Check before swapping")

    @app.post("/swap")
    def swap_apply():
        chosen = request.form.getlist("device")
        if not chosen:
            return redirect(_u("index"))

        registry, plans, problems = swap_plans(chosen)
        store = ProvenanceStore(store_path)

        # Pairings the person chose on the preview, keyed by cloud entity.
        choices = {
            key[len("pair__") :]: value
            for key, value in request.form.items()
            if key.startswith("pair__") and value
        }

        done, failed = [], list(problems)
        for tuya_id, plan in plans:
            for cloud_id, why in add_manual_pairs(plan, choices).items():
                failed.append((cloud_id, why))
            try:
                results = apply_swap(registry, plan, store, tuya_id)
            except Exception as exc:
                logger.exception("swap failed for %s", tuya_id)
                failed.append((tuya_id, str(exc)))
                continue
            for result in results:
                (done if result.ok else failed).append(
                    (result.entity_id, result.detail)
                    if result.ok
                    else (result.entity_id, result.detail or "failed")
                )
        store.save()
        return page(_render_swap_result(done, failed), "Entity ids moved")

    # ── Auto-heal ──────────────────────────────────────────────────────────

    def _heal_once() -> None:
        """Re-sync any converted entry whose address, key or protocol moved.

        This has to run here rather than anywhere else: converting a device
        consumes its Home Assistant discovery flow, so a converted device is
        invisible to everything except a UDP scan on its own network. Run from
        outside, drift detection reports "nothing has drifted" for exactly the
        devices it exists to fix.
        """
        from .heal import (
            DirectOptionsFlowClient,
            HealError,
            detect_drift,
            entry_ids_for_devices,
            repair,
        )

        if not (ha_url and ha_token):
            logger.warning("auto-heal needs a direct Home Assistant connection")
            return
        if not os.path.exists(session_path):
            logger.debug("auto-heal: not logged in yet")
            return

        session = cloud_mod.TuyaCloudSession.load(session_path)
        cloud_devices = session.devices()
        lan_devices, _flows, _converted = discovery()
        store = ProvenanceStore(store_path)

        registry = ha_discovery.device_registry_direct(ha_url, ha_token)
        drifts = detect_drift(
            cloud_devices, lan_devices, store, entry_ids_for_devices(registry)
        )
        if not drifts:
            logger.debug("auto-heal: nothing has drifted")
            return

        client = DirectOptionsFlowClient(ha_url, ha_token)
        for drift in drifts:
            try:
                repair(client, drift)
                logger.info(
                    "auto-heal repaired %s (%s)",
                    drift.name or drift.device_id,
                    drift.describe(),
                )
            except HealError as exc:
                # A device that is simply switched off cannot be repaired, and
                # saying so every interval would bury anything that matters.
                logger.warning("auto-heal could not repair %s: %s", drift.device_id, exc)

        store.record_cloud(cloud_devices)
        store.record_lan(lan_devices)
        store.save()

    def _heal_loop() -> None:
        interval = max(heal_interval_hours, 0.25) * 3600
        # Let the first scan finish before the first pass, or it runs against
        # an empty picture and concludes, wrongly, that nothing has moved.
        time.sleep(min(interval, 180))
        while True:
            try:
                _heal_once()
            except Exception:
                logger.exception("auto-heal pass failed")
            time.sleep(interval)

    if heal_interval_hours > 0:
        threading.Thread(target=_heal_loop, name="tuya-auto-heal", daemon=True).start()
        logger.info("auto-heal every %.2g h", heal_interval_hours)

    return app


def _render_swap_preview(plans, problems) -> str:
    """What the swap would do, device by device, and what it needs asking.

    Everything lives in one form so a pairing chosen here travels with the
    button that applies it.
    """
    parts: list[str] = [
        '<form method="post" action="' + _u("swap_apply") + '" '
        'data-working="Moving entity ids —">'
    ]

    any_pairs = False
    any_choices = False
    notes: list[str] = []

    for tuya_id, plan in plans:
        parts.append(f'<input type="hidden" name="device" value="{_esc(tuya_id)}">')

        if plan.pairs:
            any_pairs = True
            rows = "".join(
                f"<tr><td><code>{_esc(pair.local_entity_id)}</code></td>"
                f'<td class="muted">becomes</td>'
                f"<td><code>{_esc(pair.cloud_entity_id)}</code></td></tr>"
                for pair in plan.pairs
            )
            parts.append(
                f'<div class="card wrap"><table>'
                f"<tr><th>local entity</th><th></th><th>takes this id</th></tr>"
                f"{rows}</table></div>"
            )

        # Anything the matcher would not guess at becomes a question.
        undecided = [
            (entity_id, candidates_for(plan, entity_id))
            for entity_id in plan.cloud_unmatched
        ]
        askable = [(e, c) for e, c in undecided if c]
        stranded = [e for e, c in undecided if not c]

        if askable:
            any_choices = True
            rows = "".join(
                f"<tr><td><code>{_esc(entity_id)}</code></td>"
                f'<td class="muted">taken over by</td><td>'
                f'<select name="pair__{_esc(entity_id)}">'
                '<option value="">leave it on the cloud</option>'
                + "".join(
                    f'<option value="{_esc(c)}">{_esc(c)}</option>' for c in cands
                )
                + "</select></td></tr>"
                for entity_id, cands in askable
            )
            parts.append(
                '<div class="note"><b>These need you to decide.</b> The two '
                "integrations name them differently, and guessing could put a "
                "switch on the wrong relay — so pick the local entity that "
                "matches, or leave it alone.</div>"
                f'<div class="card wrap"><table>'
                f"<tr><th>cloud entity</th><th></th><th>local entity</th></tr>"
                f"{rows}</table></div>"
            )

        if stranded:
            notes.append(
                '<div class="note"><b>Left on the cloud.</b> These have no local '
                "counterpart at all, so anything using them still goes through "
                "Tuya:<br>"
                + "<br>".join(f"<code>{_esc(e)}</code>" for e in stranded)
                + "</div>"
            )

    if any_pairs or any_choices:
        parts.insert(
            1,
            "<p>The local entity takes the id the cloud one had, so anything "
            "referring to it keeps working untouched. The cloud entity is "
            "renamed with a <code>_cloud</code> suffix and disabled.</p>",
        )
        parts.append("<button>Move the ids</button>")
    parts.append("</form>")
    parts.extend(notes)

    if not (any_pairs or any_choices):
        # Nothing can be done, but why is worth saying: a device whose cloud
        # entities have no local counterpart is a different situation from one
        # that is already swapped, and the notes are the only thing that says
        # which this is.
        parts = notes or ['<div class="note">Nothing to swap.</div>']

    if problems:
        parts.append(
            '<h2>Cannot swap</h2><div class="card wrap"><table>'
            + "".join(
                f"<tr><td><code>{_esc(str(d))}</code></td>"
                f'<td class="muted">{_esc(str(why))}</td></tr>'
                for d, why in problems
            )
            + "</table></div>"
        )

    parts.append(f'<p><a href="{_u("index")}">Back to status</a></p>')
    return "".join(parts)


def _render_swap_result(done, failed) -> str:
    rows = "".join(
        f"<tr><td><code>{_esc(str(e))}</code></td>"
        f'<td class="ok">moved</td><td class="muted">{_esc(str(d))}</td></tr>'
        for e, d in done
    ) + "".join(
        f"<tr><td><code>{_esc(str(e))}</code></td>"
        f'<td class="err">failed</td><td class="muted">{_esc(str(d))}</td></tr>'
        for e, d in failed
    )
    note = (
        '<div class="note">Every move is recorded and can be undone with '
        "<code>tuya-local-bridge rollback</code>.</div>"
        if done
        else ""
    )
    return (
        f'<div class="card wrap"><table>'
        f"<tr><th>entity</th><th>result</th><th></th></tr>{rows}</table></div>"
        f"{note}"
        f'<p><a href="{_u("index")}">Back to status</a></p>'
    )


# ── rendering helpers ──────────────────────────────────────────────────────


# Paths as they are without ingress. The render helpers below are unit-tested
# directly, with no application around them, and that is worth keeping: their
# job is HTML, not routing. So ask Flask for the URL when there is a request
# to ask about, and fall back to the plain path when there is not.
_PLAIN_PATHS = {
    "index": "/",
    "login_form": "/login",
    "login_start": "/login",
    "login_finish": "/login/finish",
    "convert_confirm": "/convert/confirm",
    "convert_start": "/convert",
    "convert_finish": "/convert/finish",
    "swap_confirm": "/swap/confirm",
    "swap_apply": "/swap",
}


def _u(endpoint: str, **values: Any) -> str:
    try:
        return url_for(endpoint, **values)
    except RuntimeError:
        path = _PLAIN_PATHS[endpoint]
        if values:
            path += "?" + "&".join(f"{k}={v}" for k, v in values.items())
        return path


def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_status(
    result,
    flows: dict[str, str],
    rotated: list[str],
    *,
    scan_enabled: bool = False,
    swapped: frozenset[str] = frozenset(),
    scanning: bool = False,
    scan_error: str | None = None,
    scan_elapsed: int = 0,
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

    if scanning:
        # The counter matters: a still "Scanning..." with no number is what a
        # hung page looks like, which is the complaint this replaces.
        parts.append(
            '<div class="note"><div class="scanning">'
            '<div class="spinner"></div>'
            f"<div>Scanning the network for devices — {scan_elapsed}s. "
            "The list below is what we know so far; it will fill in on its own."
            "</div></div></div>"
        )
    elif scan_error:
        parts.append(
            f'<div class="note"><b>The network scan failed:</b> {_esc(scan_error)}.<br>'
            "Home Assistant's own discovery is still being used, so devices it "
            "has already seen are listed; devices already converted may be "
            "missing.</div>"
        )
    elif scan_enabled:
        parts.append(
            f'<p><a href="{_u("index")}?rescan=1">Rescan the network</a> '
            '<span class="muted">(runs in the background; the page updates '
            "itself)</span></p>"
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
            '<h2>Ready to convert</h2><form method="post" action="' + _u("convert_confirm") + '">'
            f'<div class="card wrap"><table>'
            f"<tr><th></th><th>name</th><th>address</th><th>device id</th><th>type</th></tr>"
            f"{rows}</table></div>"
            "<button>Convert selected&hellip;</button></form>"
        )

    if result.converted:
        parts.append(
            "<h2>Already on tuya-local</h2>"
            '<p class="muted">Converted earlier. If their entity ids were never '
            "moved across, anything referring to them is still pointing at the "
            "cloud entity.</p>"
            '<form method="post" action="' + _u("swap_confirm") + '">'
            '<div class="card wrap"><table>'
            "<tr><th></th><th>name</th><th>device id</th><th></th></tr>"
            + "".join(
                "<tr><td>"
                + (
                    '<span class="ok" title="entity ids already moved">&#10003;</span>'
                    if c.id in swapped
                    else f'<input type="checkbox" name="device" value="{_esc(c.id)}">'
                )
                + f"</td><td>{_esc(c.name)}</td>"
                f"<td><code>{_esc(c.id)}</code></td>"
                f'<td class="muted">'
                + ("ids moved" if c.id in swapped else ("online" if c.online else "offline"))
                + "</td></tr>"
                for c in result.converted
            )
            + "</table></div>"
            + (
                "<button>Move the entity ids across</button>"
                if any(c.id not in swapped for c in result.converted)
                else ""
            )
            + "</form>"
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


def _render_confirm(picked) -> str:
    """What will happen, and what is still to be decided."""
    if not picked:
        return (
            '<p class="err">Nothing selected.</p>'
            f'<p><a href="{_u("index")}">Back</a></p>'
        )

    rows = "".join(
        f"<tr><td>{_esc(m.cloud.name)}</td><td><code>{_esc(m.lan.ip)}</code></td>"
        f'<td class="muted">{_esc(m.cloud.category)}</td></tr>'
        for m in picked
    )
    count = len(picked)
    plural = "device" if count == 1 else "devices"
    hidden = "".join(
        f'<input type="hidden" name="device" value="{_esc(m.id)}">' for m in picked
    )

    return (
        f"<h2>About to convert {count} {plural}</h2>"
        f'<div class="card wrap"><table>'
        "<tr><th>name</th><th>address</th><th>type</th></tr>"
        f"{rows}</table></div>"
        '<div class="card" style="padding:1rem">'
        "<h3>What happens now</h3>"
        "<ul>"
        "<li>Each device is added to <b>tuya-local</b> in Home Assistant, "
        "using the key from your Tuya account. It then works over your own "
        "network, with no cloud in the way.</li>"
        "<li>Devices are done one at a time. If one fails the rest still go "
        "ahead, and you will get a line explaining each failure.</li>"
        "<li>Nothing is removed. Your existing Tuya integration and its "
        "entities stay exactly as they are until you choose to remove "
        "them.</li>"
        "</ul>"
        "<h3>What you may still be asked</h3>"
        "<ul>"
        "<li><b>Device type.</b> tuya-local often cannot tell a dimmer from a "
        "switch, so the next screen may ask you to pick one per device. It "
        "puts its best guess first. Nothing is written for those devices "
        "until you choose.</li>"
        "<li><b>A device that has moved.</b> If one does not answer, we look "
        "for it again before giving up, which takes a minute or so.</li>"
        "</ul>"
        "<p class=\"muted\">Entity names and history are not changed by this "
        "step.</p>"
        "</div>"
        f'<form method="post" action="{_u("convert_start")}" '
        'data-working="Converting\u2026" data-slow>' + hidden +
        "<button>Yes, convert them</button></form>"
        f'<p><a href="{_u("index")}">Cancel</a></p>'
    )


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
            '<form method="post" action="' + _u("convert_finish")
            + '" data-working="Finishing\u2026">'
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

    parts.append('<p><a href="' + _u("index") + '">Back to status</a></p>')
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
