# Changelog

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
