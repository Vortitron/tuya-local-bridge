# tuya-local-bridge

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

Verified end to end against a live account and a live Home Assistant: 21 cloud
devices, 16 discovery flows, reconciled to 11 matched / 3 converted / 7
cloud-only / 5 lan-only. 41 unit tests.

Not yet done:

- **`--source lan` is untested on real hardware.** It wraps tinytuya's scanner
  but has only been exercised against captured output. `--source ha` is the
  verified path.
- **The entity swap.** Adding a tuya-local device creates *new* entities, so
  automations referencing the cloud entity break. The fix is to preserve
  `entity_id`: free it from the cloud entity, then rename the local one into
  it. `Migration.local_entity_id_original` exists to make that reversible, but
  nothing drives Home Assistant's entity registry yet.
- Home Assistant add-on packaging and web UI.

## Licence

MIT.
