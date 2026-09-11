# Tuya Local Bridge

Finds the local keys for your Tuya devices — **without a Tuya developer account
or an IoT Core subscription** — and lines them up against what
[tuya-local](https://github.com/make-all/tuya-local) can see on your network.

## What is proven, and what is not

Finding your devices and their local keys **works**, and it is the tedious half.

**Automatic conversion is not yet confirmed on a live install.** The bridge
answers every step of tuya-local's config flow, but the last of those steps is
covered by tests against a recorded flow rather than by a device actually
converted end to end. If it stops short, copy the keys it found into tuya-local
by hand — you still skip the developer portal entirely.

Please [report either outcome](https://github.com/Vortitron/tuya-local-bridge/issues).

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
