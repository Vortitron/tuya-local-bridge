# Tuya Local Bridge (Home Assistant add-on)

Finds the local keys for your Tuya devices and converts them to
[tuya-local](https://github.com/make-all/tuya-local), without a Tuya developer
account or an IoT Core subscription.

## Install

Add this repository in **Settings → Add-ons → Add-on store → ⋮ → Repositories**,
then install *Tuya Local Bridge* and open it from the sidebar.

## Use

1. Get your **User Code** from the Smart Life app: *Me → gear → Account and
   Security → User Code*.
2. Enter it in the add-on and scan the QR code with the same app.
3. Review the four groups and convert the devices you want.

## Requirements

- The **tuya-local** integration installed (via HACS).
- Devices on the same network as Home Assistant.

Zigbee and Bluetooth devices behind a hub have no individual local key and
cannot be converted — only Wi-Fi devices and the hubs themselves.

## Notes

`host_network` is enabled so the add-on can hear Tuya's UDP broadcasts. Without
it the add-on still works using Home Assistant's own discovery, but cannot see
already-converted devices or read protocol versions.

State lives in `/data`: your Tuya session token and the provenance record of
which keys were seen when. Both are private to the add-on.
