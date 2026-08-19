# tuya-local-bridge

> [!WARNING]
> **Conversion does not work end to end yet** (as of 0.1.11, Aug 2026).
>
> Discovery, cloud login and key matching work. Driving tuya-local's config
> flow does not: the flow has at least four steps (`user` → `local` →
> `select_type` → a step asking for `name`), flows persist part-answered
> between attempts, and the bridge does not yet answer the last one. A
> conversion therefore stops with "tuya-local would not accept the device
> details. Its form is asking for name".
>
> Use it to *find* your devices and their local keys; add them to tuya-local
> by hand for now.


Joins **Tuya cloud local keys** to **LAN-discovered devices**, so
[tuya-local](https://github.com/make-all/tuya-local) can be configured without
hand-copying device IDs and keys out of the Tuya developer portal.

tuya-local already discovers devices on your network. The cloud already knows
every device's `local_key`. Nothing joins the two — that is what this does.

## Why not the Tuya developer portal

The usual advice (create a cloud project, grab Access ID/Secret, call
`/v2.0/cloud/thing/{id}`) depends on an **IoT Core subscription**. That trial
lasts about a month and can only be renewed every six, after which every data
endpoint returns:

```
28841002  IoT Core service subscription has expired.
```

A fresh project does not reset it — the subscription is account-level. Anything
built on that path breaks for most users within a month.

This tool uses the **device-sharing flow** instead: read a User Code from the
Smart Life app, scan a QR, done. No developer account, no cloud project, no
subscription. It is the same mechanism Home Assistant's own Tuya integration
uses, and it returns `local_key` for every non-sub device.

## Install

```bash
pip install -e ".[lan,qr]"
```

`lan` pulls in tinytuya for discovery; `qr` renders the login code in the
terminal. Both are optional but you want them.

## Use

```bash
tuya-local-bridge login            # scan the QR with Smart Life
tuya-local-bridge status           # cloud + LAN, reconciled
tuya-local-bridge export           # config for the matched devices
```

`status` splits everything four ways:

| bucket | meaning |
| --- | --- |
| **matched** | in the cloud *and* discovered — ready for tuya-local |
| **already converted** | tuya-local already owns it |
| **cloud-only** | offline, on another subnet, or not local-capable |
| **lan-only** | discovered but unexplained — usually re-paired, so any cached key is **stale** |

That last bucket is the one that matters. A re-paired device gets a new local
key and nothing tells you; the old tuya-local entry simply stops responding,
often months later.

**"Already converted" is not cosmetic.** Adding a device to tuya-local
*consumes its discovery flow*, so a converted device stops being discoverable.
Without that bucket it is indistinguishable from one that has gone offline —
exactly backwards. It is resolved from HA's device registry, where tuya-local
stores the Tuya device id verbatim.

## Install as a Home Assistant add-on

Add this repository in **Settings → Add-ons → Add-on store → ⋮ → Repositories**,
install *Tuya Local Bridge*, and open it from the sidebar. It runs on host
networking so it gets both discovery sources, and uses the Supervisor token —
no configuration beyond your Smart Life User Code.

See [`addon/`](addon/).

## Where the LAN half comes from

Two sources, chosen with `--source`:

```bash
# Read Home Assistant's own tuya-local discovery. Works remotely.
tuya-local-bridge status --source ha --instance <id> --token <vomehome-token>

# Or straight from Home Assistant
tuya-local-bridge status --source ha-direct --ha-url http://homeassistant:8123 --token <ha-token>

# Or scan the network yourself (must be on the broadcast domain)
tuya-local-bridge status --source lan
```

`--source ha` is usually the better one. tuya-local is already resident on the
right network and already raises a discovery flow per device, recording the
Tuya device id as `unique_id` and the address in `title_placeholders` — the
`device_id -> ip` half of the join, already collected. Reading it back means
**no code has to run on the LAN at all**, it covers everything HA has heard
since boot rather than one scan window, and it cannot disagree with what the
user sees in the HA UI.

Its limit is that it only sees flows that are still pending, which is precisely
why the converted bucket exists.

### Neither source is complete — use both

They fail in opposite directions:

| | Home Assistant discovery | UDP scan |
| --- | --- | --- |
| coverage | everything heard since boot | only what is broadcasting now |
| converted devices | **invisible** (flow consumed) | visible |
| sleepy battery devices | visible (heard earlier) | **often missed** |
| protocol version | not carried | yes |
| flow id for conversion | yes | no |

Measured on one real network: **16 from HA, 15 from a scan, 18 between them.**
`merge_lan()` unions them — pass HA first so its flow ids survive while the scan
contributes protocol versions. The add-on does this automatically.

## Devices from other brands (LEDVANCE, SYLVANIA, …)

Many "not Tuya" devices are Tuya hardware sold under another brand. They pair
only in that brand's app, which is a Tuya white-label build with its own app
credentials — so the Smart Life QR flow cannot see them at all, and they land in
the **lan-only** bucket. LEDVANCE in particular has moved off the Tuya platform,
so re-pairing them to Smart Life does not work either.

Those apps use Tuya's older *mobile app* API, which returns `localKey` for an
email and password:

```bash
tuya-local-bridge vendor ledvance --email you@example.com
tuya-local-bridge status --include-stored --source ha --instance <id> --token <t>
```

The keys are folded into `provenance.json`, so the password is only used once
and never stored.

Two other routes for the same devices, no tooling required:

- Some builds of the vendor app expose the local key in device settings, but
  do not count on it — recent LEDVANCE versions do not.
- **[tuya-cloudcutter](https://github.com/tuya-cloudcutter/tuya-cloudcutter)**
  can detach BK7231/RTL devices from Tuya entirely and flash open firmware. That
  is irreversible and needs a device profile, but it removes the vendor account
  from the picture for good.

## The cloud does not give you a usable address

Every cloud device reports `ip` as your **WAN** address, not a LAN one. Feeding
it to tuya-local is a common and confusing mistake, so `CloudDevice` names the
field `wan_ip` deliberately.

The join is therefore across two sources, neither sufficient alone:

```
cloud:      device_id -> local_key        (WAN address only)
discovery:  device_id -> 192.168.x.y      (no key)
```

Devices are joined on Tuya device id, never on address — ids are stable across
DHCP changes.

## When a device stops responding

A tuya-local entry pins three things that are not constant:

- the **address**, which DHCP can move — a reservation makes that unlikely, not
  impossible;
- the **local key**, which rotates whenever the device is re-paired;
- the **protocol version**, which can change across firmware updates.

All three fail identically: the device stops responding, often months later,
with nothing in the log naming the cause. The repair is the same in each case —
re-submit what we can re-derive:

```bash
tuya-local-bridge heal --dry-run     # what has moved
tuya-local-bridge heal               # re-sync it
```

Discovery supplies the current address and protocol version, the account
supplies the current key, and the provenance store says what the entry was
configured with — so the difference *is* the staleness. This is why every
observation is timestamped rather than overwritten.

Repair needs Home Assistant's options-flow API, which the VomeHome broker does
not route yet, so it runs in direct mode (the add-on, or `--ha-url` with
`HA_TOKEN`). Detection works everywhere.

## Local keys are cached state, not configuration

Every observation is timestamped in `provenance.json`. A changed key bumps
`key_generation` and records `key_rotated_at` rather than overwriting silently,
and `stale_keys()` surfaces migrated devices whose key has not been
re-confirmed. Re-run `status` periodically; that is the re-sync.

The store also records which cloud entity a local entity replaced, so a
migration can be rolled back.

## Running it

Only `--source lan` needs to be **inside the broadcast domain**. `--source ha`
works from anywhere, because Home Assistant did the listening.

The login endpoint is `/v1.0/m/life/home-assistant/qrcode/tokens` and it takes
Home Assistant's client id. It is a Tuya↔Home Assistant surface, so this is
intended to run inside Home Assistant, as an add-on or custom component. Do not
call it from unrelated hosted infrastructure.

## State

`~/.config/tuya-local-bridge/` (override with `TUYA_LOCAL_BRIDGE_DIR`):

- `session.json` — refresh token. Long-lived and grants **full control of your
  home**. Written `0600`. Revoke via Smart Life → Me → Account → Device Sharing.
- `provenance.json` — keys and migration history. Also `0600`.

## Status

Verified end to end against live accounts and a live Home Assistant.

The Smart Life account gave 21 devices; Home Assistant's discovery gave 16
flows; a UDP scan cross-checked both. That reconciled to 11 matched, 3 already
converted, 7 not discovered — and 5 the account could not explain.

Those five turned out to be LEDVANCE bulbs. Adding the vendor-app provider
resolved every one of them:

```
matched 16  |  already converted 3  |  cloud-only 9  |  lan-only 0
```

An empty lan-only bucket is the goal: every device on the network accounted
for, and every convertible one holding a usable key. 103 unit tests.

Not yet done:

- **The add-on image has not been built.** The manifests are written and
  validated but nothing has run `docker build` against a Home Assistant base
  image, and the Dockerfile installs from git, so the repo must be pushed first.
- **The entity swap.** Adding a tuya-local device creates *new* entities, so
  automations referencing the cloud entity break. The fix is to preserve
  `entity_id`: free it from the cloud entity, then rename the local one into
  it. `Migration.local_entity_id_original` exists to make that reversible, but
  nothing drives Home Assistant's entity registry yet.

## Licence

MIT.
