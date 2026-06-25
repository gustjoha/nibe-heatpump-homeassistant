#!/usr/bin/with-contenv bashio
# ==============================================================================
# Nibe Smart Control - Entrypoint
# ==============================================================================
set -e

bashio::log.info "Starting Nibe Smart Control addon..."
bashio::log.info "Version: $(cat /etc/nibe_smart_control_version 2>/dev/null || echo 'unknown')"

exec python3 /usr/bin/nibe_smart_control.py
