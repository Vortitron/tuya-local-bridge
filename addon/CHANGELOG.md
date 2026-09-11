# Changelog

## 0.1.18

- **Plugs can have their entity ids moved.** Tuya's cloud calls a plug's only
  switch "Socket 1" while tuya-local leaves the primary entity unnamed, so
  matching on the name reported "no entries to match with" for the most
  common device there is. Entities now also pair on the domain alone where it
  is unambiguous — exactly one on each side. Where a domain holds several of
  either, they are still reported rather than guessed at, because putting
  readings on the wrong sensor is worse than saying nothing.

## 0.1.17

- **Auto-heal**, on by default every 6 hours (`heal_interval_hours`, 0 to turn
  off). A tuya-local entry pins an address, a local key and a protocol
  version, none of which stay put; when one moves the device goes quiet with
  nothing in the log naming the cause. This re-derives all three and re-syncs
  the entry.
- **The network scan no longer blocks the page.** It ran inline, so the first
  load simply did not answer for most of a minute — indistinguishable from a
  hung request. It now runs in the background, the page says it is scanning
  and counts the seconds, and refreshes itself when there is more to show.
  "Rescan" returns immediately for the same reason.
- **The running version is shown at the bottom of every page**, so "did my
  update actually land?" is answerable without leaving it.

## 0.1.16

- **Already-converted devices can have their entity ids moved too.** The offer
  after a conversion only helped devices converted from then on; anything
  converted earlier was stuck. The "Already on tuya-local" list now has a
  checkbox per device and the same preview.
- Devices whose ids have already been moved show a tick instead of a checkbox,
  so the list says what is done rather than inviting it twice.

## 0.1.15

- **Moving entity ids across is now offered in the web UI**, right after a
  conversion. Converting mints new entities, so automations, scripts and
  dashboards still point at the old cloud ones and quietly stop working —
  which made the conversion only half the job. The local entity takes the id
  the cloud one had, so nothing referring to it needs editing at all.
- It is previewed before anything moves, and says which entities have no local
  counterpart and so stay on the cloud. Every move can still be rolled back.

## 0.1.14

- Answers tuya-local's closing step after it was renamed from `name` to
  `choose_entities` in 2026.9.0, which stopped conversion one form short of a
  device with "unhandled step 'choose_entities'".
- That step is now recognised by its shape — a form asking for nothing but a
  name — rather than by its id, so the next rename should not break it.
- **A device has now been converted end to end on a live install.**

## 0.1.13

- Drops **armv7**. Home Assistant removed support for it in 2025.12, so it
  could not be published for that architecture anyway.

- Installs and updates now **pull a prebuilt image** instead of building one on
  your machine. Building locally meant every install depended on apk, PyPI and
  a `git clone` from inside your Docker build, none of which time out — when
  one stalled, the install sat at 0% with no error and survived a Home
  Assistant restart, because add-on jobs belong to the Supervisor rather than
  Core.

## 0.1.12

- Works without VomeHome. A direct Home Assistant connection is now the
  default everywhere, and every command resolves it the same way — direct
  users previously lost the "already converted" group, which makes a
  converted device look offline.
- Answers tuya-local's closing `name` step, which is what left conversion one
  form short of a device. **Still unconfirmed on a live install** — please
  report either outcome.
- The add-on release checks no longer skip silently when PyYAML is missing,
  which is how a stale package pin reached a release.

## 0.1.11

- Say what is happening while converting. It talks to Home Assistant device by
  device and scans the subnet for any that moved, which can run past a minute
  with nothing on screen.

## 0.1.10

- Resume a config flow that is already part way through, rather than failing
  on a step that was answered in an earlier attempt.

## 0.1.9

- Read what each flow step is asking for instead of assuming its shape, and
  stop trusting an address that several devices claim — that is the gateway
  forwarding broadcasts, not the device.

## 0.1.8

- Find a device that has changed address instead of failing on the old one.

## 0.1.7

- Read `setup_mode` options from the selector, checked against a live flow.

## 0.1.6

- Read the body of a 400 rather than raising on it, so the reason reaches you.

## 0.1.5

- Answer tuya-local's `setup_mode` step before sending device fields.

## 0.1.4

- Read discovery flows over WebSocket when the REST proxy refuses.

## 0.1.3

- Pin the package to a commit. Docker caches the install layer on the text of
  the command, so tracking a branch meant a rebuild reinstalled the *old*
  package: the version went up, the code did not.

## 0.1.2

- Serve correctly behind Home Assistant ingress.

## 0.1.1

- Pick up the `build.yaml` fix.

## 0.1.0

- First release: find local keys, reconcile them against what is on the
  network, and convert to tuya-local.
