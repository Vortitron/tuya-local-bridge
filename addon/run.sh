#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

# Supervisor gives us a token that is good for both the REST and WebSocket APIs.
export TUYA_LOCAL_BRIDGE_DIR=/data
export HA_TOKEN="${SUPERVISOR_TOKEN}"

SCAN_SECONDS="$(bashio::config 'scan_seconds')"
HEAL_HOURS="$(bashio::config 'heal_interval_hours')"
bashio::log.info "Starting Tuya Local Bridge on :8099 (scan ${SCAN_SECONDS}s, heal every ${HEAL_HOURS}h)"

exec tuya-local-bridge --dir /data serve \
    --host 0.0.0.0 \
    --port 8099 \
    --ha-url "http://supervisor/core" \
    --scan-seconds "${SCAN_SECONDS}" \
    --heal-interval-hours "${HEAL_HOURS}"
