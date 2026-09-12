# Changelog

## 0.1.25

- **The device name now follows the entity ids.** The local device takes the
  cloud device's name and the cloud one gains a `(cloud)` suffix, so two
  identically named devices no longer sit side by side in every picker.
  Devices swapped before this existed are offered a catch-up on the status
  page.
- **Learning a key is no longer reported as losing one.** A record is created
  the moment a device is heard on the network, before any account has been
  consulted, so the first key ever fetched was announced as "rotated — your
  entry is dead". Five LEDVANCE bulbs said exactly that, at the instant their
  keys were first read.

## 0.1.24

- **Says when a key can no longer be vouched for.** A Smart Life key is re-read
  on every refresh, so a rotated one is caught within minutes. A key from a
  LEDVANCE or SYLVANIA account is read once, when you typed that password, and
  nothing re-reads it — so if such a device is re-paired, its key changes and
  the only symptom is the device going quiet weeks later. The status page now
  names those devices, says which account would settle it, and links to a
  sign-in form already filled in.

## 0.1.23

- **Devices from another brand can now actually be converted.** They appeared
  on the status page and then vanished the moment you pressed a button —
  "nothing selected" for a device plainly listed and ticked. Only the status
  page folded in keys fetched from a vendor account; every page that acted
  rebuilt the picture from the Smart Life session alone, where those devices
  do not exist. One place now decides what devices there are.
- Auto-heal covers them too. A vendor key cannot be re-fetched without the
  password, but addresses and protocol versions drift like anything else.

## 0.1.22

- **Other brands can be signed in to from the add-on.** LEDVANCE, SYLVANIA and
  other white-label Tuya apps are separate accounts the Smart Life login
  cannot see, so their devices sat in the unexplained list for ever. There is
  now a form beside that list; the password is used once and never stored, and
  the keys join the reconciliation like any others.
- **Undoing an id move is a button.** Both it and the swap were command-line
  only, which is no use to anyone running the add-on — and an undo you cannot
  reach is not really an undo.
- The undo preview says the thing people will not have thought of: putting the
  ids back means anything referring to them is talking to the cloud again.

## 0.1.21

- **A choice made for one device is no longer offered to another.** Swapping
  several devices at once sent every answer to every device, so the ones it
  was not meant for refused it — reporting "failed: no longer needs a pairing"
  next to the move that had just succeeded. Nothing was actually wrong with
  the swap; the report was.

## 0.1.20

- **Where the matcher will not guess, it now asks.** A plug's cloud switches
  are "Socket 1" and "Child lock" while tuya-local calls its own nothing and
  "Overcharge protection" — nothing to match on, and guessing could put a
  switch on the wrong relay. The preview now offers a dropdown of the sensible
  local entities for each one, so the main switch can be paired in one click
  instead of being refused.
- Entities with no local counterpart at all still say so plainly rather than
  being offered a choice that does not exist.
- The add-on description no longer says conversion is unconfirmed. It has been
  confirmed on a live install since 0.1.14, and the add-on page had been
  saying otherwise ever since.

## 0.1.19

- **Finds the right device when several claim the same Tuya id.** An
  identifier is a claim rather than a title: any integration can attach itself
  to a device by reusing it, and helpers that derive energy sensors do exactly
  that. Three devices shared one id on a real install, and the bridge picked
  the helper — so it hunted for a plug's switch among generated energy sensors
  and reported "nothing pairs up to swap" for a device that had four perfectly
  good pairs. It now picks the device whose own config entry belongs to the
  integration in question.

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
