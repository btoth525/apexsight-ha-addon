#!/usr/bin/with-contenv bashio
# Exports the add-on options as env vars and launches the relay (uvicorn).
# Secrets (the APNs .p8 + Key ID) are NOT set here — they're uploaded through the
# web GUI and stored in the add-on's persistent /data volume.

export APEX_ADMIN_USERNAME="$(bashio::config 'admin_username')"
export APEX_ADMIN_PASSWORD="$(bashio::config 'admin_password')"
export APEX_PORT="3421"
export APEX_DATA_DIR="/data"
export APEX_TEAM_ID="3Q9ZUDN4QZ"
export APEX_BUNDLE_ID="com.brandontoth.apexsight.native"

if bashio::config.is_empty 'admin_password'; then
  bashio::log.warning "admin_password is empty — set it in Configuration before using the web GUI."
fi

bashio::log.info "ApexSight Push Relay starting on container port ${APEX_PORT}"
cd /srv
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${APEX_PORT}"
