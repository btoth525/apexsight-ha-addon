#!/usr/bin/with-contenv bashio
# Reads addon options + the Home Assistant MQTT service, exports them, and
# launches the Python bridge.

export RELAY_URL="$(bashio::config 'relay_url')"
export PAIRING_CODE="$(bashio::config 'pairing_code')"
export FRIGATE_BASE_URL="$(bashio::config 'frigate_base_url')"
export TOPIC="$(bashio::config 'topic')"
export ALERTS_ONLY="$(bashio::config 'alerts_only')"

# MQTT: prefer the auto-provided HA broker, allow manual override in options.
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

bashio::log.info "ApexSight Push Bridge starting → relay ${RELAY_URL}, topic ${TOPIC}"
exec python3 /bridge.py
