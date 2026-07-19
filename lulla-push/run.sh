#!/usr/bin/with-contenv bashio
# Lulla Push + Sync entrypoint. Reads HA add-on options → env, then launches uvicorn.
set -e

export PORT="6969"
export LULLA_DATA_DIR="/data"
export PAIRING_CODE="$(bashio::config 'pairing_code')"
export ADMIN_USERNAME="$(bashio::config 'admin_username')"
export ADMIN_PASSWORD="$(bashio::config 'admin_password')"
export LOG_LEVEL="$(bashio::config 'log_level')"
# Push routing policy (§7.4) — surfaced to the FastAPI app via app/config.py.
export QUIET_HOURS_START="$(bashio::config 'quiet_hours_start')"
export QUIET_HOURS_END="$(bashio::config 'quiet_hours_end')"
export NAP_AWARE="$(bashio::config 'nap_aware')"

bashio::log.info "Starting Lulla Push + Sync on :${PORT} (household ${PAIRING_CODE})"

exec python3 -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --log-level "${LOG_LEVEL:-info}"
