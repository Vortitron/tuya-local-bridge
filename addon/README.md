# Tuya Local Bridge

Finds the local keys for your Tuya devices — **without a Tuya developer account
or an IoT Core subscription** — and lines them up against what
[tuya-local](https://github.com/make-all/tuya-local) can see on your network.

## What it does

1. Finds every Tuya device on your account and its local key, from a QR-code
   login — no developer account, no cloud project, no subscription.
2. Matches those against what is actually on your network.
3. Converts them to tuya-local, and **moves the entity ids across** so your
   automations keep working rather than quietly pointing at a dead device.

Converting has been confirmed end to end on a live install. Where the two
integrations name things differently the bridge asks rather than guesses, so
some devices need one choice from you before the ids move.

## Use

1. Get your **User Code** from the Smart Life app:
   *Me → gear icon → Account and Security → User Code*.
2. Enter it here and scan the QR code with the same app.
3. Review the four groups and convert the devices you want.

## Requirements

- The **tuya-local** integration, installed via HACS.
- Devices on the same network as Home Assistant.

Zigbee and Bluetooth devices behind a hub have no individual local key and
cannot be converted — only Wi-Fi devices and the hubs themselves.

## If a device is on your network but not in your account

It is most likely Tuya hardware sold under another brand (LEDVANCE, SYLVANIA
and many others) and paired in that brand's own app, which is a separate Tuya
account. The main
[README](https://github.com/Vortitron/tuya-local-bridge#devices-sold-under-another-brand-ledvance-sylvania-)
covers how to reach those.

## Options

| option | meaning |
| --- | --- |
| `scan_seconds` | how long to listen for device broadcasts (0 disables scanning) |
| `heal_interval_hours` | how often to re-sync entries whose address, key or protocol has moved (0 disables) |
| `log_level` | add-on log verbosity |

## Notes

`host_network` is enabled so the add-on can hear Tuya's UDP broadcasts. Without
it the add-on still works using Home Assistant's own discovery, but cannot see
already-converted devices or read protocol versions.

State lives in `/data`: your Tuya session token and a record of which keys were
seen when. The session token is long-lived and grants full control of your Tuya
account — revoke it from Smart Life → *Me → Account and Security → Device
Sharing* if you remove the add-on.
