# Tuya Local Bridge

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FVortitron%2Ftuya-local-bridge)
[![CI](https://github.com/Vortitron/tuya-local-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/Vortitron/tuya-local-bridge/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

**Finds the local keys for your Tuya devices, without a Tuya developer
account**, and lines them up against what
[tuya-local](https://github.com/make-all/tuya-local) can see on your network.

tuya-local already discovers Tuya devices on your LAN. Tuya's cloud already
knows every device's `local_key`. Nothing joined the two, so people copied IDs
and keys by hand, one device at a time, out of a developer portal that expires.

> [!NOTE]
> **Converting works, and is newly proven.** A floodlight was converted end to
> end on a live install on 11 September 2026: local entities created, reading
> the device over the LAN, no developer account anywhere in the process.
>
> It is still young. tuya-local's config flow has changed shape twice in recent
> releases — a `setup_mode` step appeared, and the closing step was renamed
> from `name` to `choose_entities` — and each time conversion stopped one form
> short until the bridge caught up. The bridge now recognises that closing step
> by its shape rather than its name, so a rename alone should not break it
> again. If yours stops on an unhandled step, that is a bug worth
> [reporting](https://github.com/Vortitron/tuya-local-bridge/issues) — and
> meanwhile the keys it found can be pasted into tuya-local by hand.

## Why not the Tuya developer portal

Every other guide tells you to create a Tuya IoT project and call the device
API. That depends on an **IoT Core subscription**, and every data endpoint
starts returning:

```
28841002  IoT Core service subscription has expired.
```

The trial lasts about a month, renews only every six, and is **account-level** —
a fresh project does not reset it. Anything built on that path breaks for most
people within a month of them setting it up.

This uses the **device-sharing flow** instead: read a User Code out of the Smart
Life app, scan a QR code, done. No developer account, no cloud project, no
subscription. It is the same mechanism Home Assistant's own Tuya integration
uses, and it returns a local key for every non-sub device.

## Install

### As a Home Assistant add-on (easiest)

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FVortitron%2Ftuya-local-bridge)

Or by hand: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
`https://github.com/Vortitron/tuya-local-bridge`, then install **Tuya Local
Bridge** and open it from the sidebar.

Installing pulls a prebuilt image, so there is nothing to compile on your
machine and it takes seconds. It runs on host networking so it can hear device
broadcasts, and authenticates with the Supervisor token — there is nothing to
configure beyond your Smart Life User Code.

> This is an **add-on**, not a HACS integration, so it is added as an add-on
> repository rather than through HACS. HACS does not install add-ons.
> You will also want [tuya-local](https://github.com/make-all/tuya-local)
> itself, which *is* a HACS integration.

### As a command-line tool

```bash
pip install "tuya-local-bridge[lan,qr] @ git+https://github.com/Vortitron/tuya-local-bridge"
```

`lan` adds tinytuya for network scanning, `qr` renders the login code in your
terminal. Add `web` for the browser UI.

## Use

```bash
tuya-local-bridge login      # scan the QR with Smart Life
tuya-local-bridge status     # what you have, and what can be converted
tuya-local-bridge export     # ready-to-paste config for the matched devices
```

`status` sorts everything four ways, and the sorting is the useful part:

| bucket | meaning |
| --- | --- |
| **matched** | in your account *and* visible on the network — ready to convert |
| **already converted** | tuya-local already has it |
| **cloud-only** | offline, on another subnet, or not local-capable |
| **lan-only** | on your network, but your account has never heard of it |

An empty **lan-only** bucket is the goal: every device on the network accounted
for. If yours is not empty, read on.

## Devices sold under another brand (LEDVANCE, SYLVANIA, …)

A great many "not Tuya" devices *are* Tuya hardware, sold under someone else's
brand and paired in that brand's own app — a Tuya white-label build with its own
account. The Smart Life QR flow cannot see them at all, so they land in
**lan-only**. LEDVANCE has since moved off the Tuya platform entirely, so
re-pairing them to Smart Life does not work either. It was never your mistake.

Those apps use Tuya's older mobile API, which returns `localKey` for an email
and password:

```bash
tuya-local-bridge vendor ledvance --email you@example.com
tuya-local-bridge status --include-stored
```

The password is used once and never stored; the keys land in the local
provenance file. LEDVANCE and SYLVANIA are supported — open an issue with a
brand and we will look at adding it.

If you would rather remove the vendor account from the picture for good,
[tuya-cloudcutter](https://github.com/tuya-cloudcutter/tuya-cloudcutter) can
detach BK7231/RTL devices from Tuya and flash open firmware. That is
irreversible and needs a device profile, but it is the thorough answer.

## How the join works

Two sources, and **neither is sufficient alone**:

```
your Tuya account:  device id -> local key      (but only your WAN address)
network discovery:  device id -> 192.168.x.y    (but no key)
```

The cloud reports `ip` as the **public** address your device last phoned home
from, not something you can connect to. Feeding that to tuya-local is the
classic mistake, so the field is called `wan_ip` in the code to stop anyone
making it twice.

Devices are joined on Tuya device id, never on address — ids survive a DHCP
lease, addresses do not.

### Two discovery sources, failing in opposite directions

| | Home Assistant's discovery | network scan |
| --- | --- | --- |
| coverage | everything heard since boot | only what is broadcasting now |
| already-converted devices | **invisible** (flow consumed) | visible |
| sleepy battery devices | visible (heard earlier) | **often missed** |
| protocol version | not carried | yes |

Measured on one real network: **16 from Home Assistant, 15 from a scan, 18
between them.** The add-on uses both and merges them.

This is also why "already converted" is its own bucket rather than a detail:
adding a device to tuya-local *consumes its discovery entry*, so a converted
device stops being discoverable. Without that bucket it is indistinguishable
from one that has gone offline — exactly backwards.

## When a device stops responding, months later

A tuya-local entry pins three things that are not constant:

- the **address**, which DHCP can move — a reservation makes that unlikely,
  not impossible;
- the **local key**, which rotates whenever the device is re-paired;
- the **protocol version**, which can change with firmware.

All three fail identically: silence, with nothing in the log naming the cause.
Because all three can be re-derived, the repair is the same in each case:

```bash
tuya-local-bridge heal --dry-run   # what has moved
tuya-local-bridge heal             # re-sync it
```

Every observation is timestamped rather than overwritten, so the difference
between what was recorded and what is there now *is* the breakage.

## Keeping your automations working

Converting a device creates **new** entities, so every automation, script and
dashboard still points at the old cloud ones. Nothing warns you; things just
quietly stop happening.

```bash
tuya-local-bridge swap --dry-run <device-id>
```

This moves the entity id across, so `light.front_porch` keeps meaning what it
always meant.

**Your recorded history follows the old entity, not the id.** Home Assistant
migrates history when an entity is renamed, so the cloud device's past lives on
under `light.front_porch_cloud` and the graph on the swapped device starts from
the conversion. Nothing is lost, but it is no longer under the name you expect. Always dry-run first: if you converted a device by hand and gave
it a better name than the cloud did, a swap would undo that. Nothing is swapped
unless you name it or pass `--all`, and every swap can be rolled back.

## Where your credentials live

`~/.config/tuya-local-bridge/` (or `/data` in the add-on):

- `session.json` — a Tuya refresh token. Long-lived, and it grants **full
  control of your home**. Written `0600`. Revoke it from Smart Life →
  *Me → Account and Security → Device Sharing*.
- `provenance.json` — local keys and migration history. Also `0600`.

Nothing is sent anywhere except Tuya's own API and your Home Assistant.

The login endpoint is a Tuya↔Home Assistant surface and takes Home Assistant's
client id, so this is meant to run inside Home Assistant, as an add-on. Please
do not point it at unrelated hosted infrastructure.

## Running against a remote Home Assistant

By default the tool talks to Home Assistant directly:

```bash
tuya-local-bridge status --source ha --ha-url http://homeassistant:8123 --token <token>
```

It can also reach an instance through the [VomeHome](https://vome.io) relay,
which is where this came from — but that is entirely optional and nothing here
needs a Vome account, a Vome server, or an internet connection beyond Tuya's
own API. If you have never heard of Vome, ignore it; everything above works
without it.

## Contributing

Issues and pull requests welcome, particularly:

- **a conversion that worked, or did not** — the open question above;
- **another brand's app credentials**, to widen the vendor support;
- **device types tuya-local guesses wrongly**.

```bash
pip install -e ".[dev]"
pytest -q
ruff check tuya_local_bridge tests
```

## Licence

MIT. Protocol details for the vendor-app path derive from
[FlagX/ha-ledvance-tuya-resync-localkey](https://github.com/FlagX/ha-ledvance-tuya-resync-localkey)
(MIT).

Not affiliated with Tuya, LEDVANCE, SYLVANIA or Home Assistant.
