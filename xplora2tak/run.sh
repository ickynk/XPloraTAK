#!/usr/bin/with-contenv bashio
# ==============================================================================
# Start the Xplora -> MQTT / TAK bridge
# ==============================================================================
bashio::log.info "Starting Xplora -> MQTT / TAK bridge..."

exec /opt/venv/bin/python3 /app/main.py
