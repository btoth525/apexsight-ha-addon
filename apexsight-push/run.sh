#!/usr/bin/with-contenv bashio
# All-in-one: runs the relay (uvicorn) AND the Frigate bridge in one container.
# The bridge posts to the relay locally on 127.0.0.1:3421.

# ---- shared API token -------------------------------------------------------
# Every public /v1 call (from the iOS app + the local bridge) must present this
# bearer token, so the relay is not controllable by anyone who merely knows the
# pairing code. Use the value from the add-on options if set; otherwise generate
# one once and persist it to /data so it is stable across restarts.
API_TOKEN="$(bashio::config 'api_token')"
TOKEN_FILE="/data/api.token"
if [ -z "${API_TOKEN}" ]; then
  if [ -f "${TOKEN_FILE}" ]; then
    API_TOKEN="$(cat "${TOKEN_FILE}")"
  else
    API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf '%s' "${API_TOKEN}" > "${TOKEN_FILE}"
    chmod 600 "${TOKEN_FILE}"
    bashio::log.info "Generated a relay API token — copy it from the web GUI dashboard into the ApexSight app."
  fi
fi

# ---- relay env --------------------------------------------------------------
export APEX_ADMIN_USERNAME="$(bashio::config 'admin_username')"
export APEX_ADMIN_PASSWORD="$(bashio::config 'admin_password')"
export APEX_API_TOKEN="${API_TOKEN}"
export APEX_COOKIE_SECURE="$(bashio::config 'secure_cookies')"
export APEX_PORT="3421"
export APEX_DATA_DIR="/data"
export APEX_TEAM_ID="3Q9ZUDN4QZ"
export APEX_BUNDLE_ID="com.brandontoth.apexsight.native"

# ---- bridge env -------------------------------------------------------------
export RELAY_URL="http://127.0.0.1:3421"
export RELAY_TOKEN="${API_TOKEN}"
export PAIRING_CODE="$(bashio::config 'pairing_code')"
export FRIGATE_BASE_URL="$(bashio::config 'frigate_base_url')"
export TOPIC="$(bashio::config 'topic')"
export ALERTS_ONLY="$(bashio::config 'alerts_only')"

if bashio::services.available "mqtt"; then
  export MQTT_HOST="$(bashio::services mqtt 'host')"
  export MQTT_PORT="$(bashio::services mqtt 'port')"
  export MQTT_USER="$(bashio::services mqtt 'username')"
  export MQTT_PASSWORD="$(bashio::services mqtt 'password')"
fi
if [ -n "$(bashio::config 'mqtt_host')" ]; then export MQTT_HOST="$(bashio::config 'mqtt_host')"; fi
if [ "$(bashio::config 'mqtt_port')" != "0" ] && [ -n "$(bashio::config 'mqtt_port')" ]; then export MQTT_PORT="$(bashio::config 'mqtt_port')"; fi
if [ -n "$(bashio::config 'mqtt_user')" ]; then export MQTT_USER="$(bashio::config 'mqtt_user')"; fi
if [ -n "$(bashio::config 'mqtt_password')" ]; then export MQTT_PASSWORD="$(bashio::config 'mqtt_password')"; fi

if bashio::config.is_empty 'admin_password'; then
  bashio::log.warning "admin_password is empty — set it in Configuration to use the web GUI."
fi

cd /srv

bashio::log.info "Starting ApexSight relay on :${APEX_PORT}"
python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${APEX_PORT}" &

sleep 3
bashio::log.info "Starting ApexSight Frigate bridge → ${RELAY_URL}, topic ${TOPIC}"
python3 /bridge.py &

# If either component exits, stop so the Supervisor restarts the whole add-on.
wait -n
bashio::log.warning "A component exited — restarting add-on."
exit 1
