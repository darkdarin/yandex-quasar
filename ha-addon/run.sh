#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

# Without the Supervisor (plain Docker, see docker-compose.yml) there are no app options: settings come from
# QUASAR_* variables, and /data holds the API token, the station registry and the backend seed files.
if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    export QUASAR_DATA_DIR="${QUASAR_DATA_DIR:-/data}"
    export QUASAR_SEED_DIR="${QUASAR_SEED_DIR:-${QUASAR_DATA_DIR}/seed}"
    mkdir -p "$QUASAR_DATA_DIR" "$QUASAR_SEED_DIR"
    cd "$QUASAR_DATA_DIR" || exit 1
    exec python3 -m quasar_adapter serve -c "$QUASAR_DATA_DIR/config.toml"
fi

export QUASAR_DATA_DIR=/data
export QUASAR_PORT="$(bashio::config 'port')"
export QUASAR_MIC_GAIN_DB="$(bashio::config 'mic_gain_db')"
export QUASAR_SPEECH_THRESHOLD_DB="$(bashio::config 'speech_threshold_db')"
export QUASAR_SPOTTER_TAIL_MS="$(bashio::config 'spotter_tail_ms')"
export QUASAR_LOG_LEVEL="$(bashio::config 'log_level')"
export QUASAR_SEED_DIR=/config
export QUASAR_GLAGOL_ACCEPT_ANY="$(bashio::config 'glagol_accept_any')"
if bashio::config.has_value 'account_uid'; then
    export QUASAR_ACCOUNT_UID="$(bashio::config 'account_uid')"
fi
if bashio::config.true 'debug_capture'; then
    export QUASAR_BACKEND_CAPTURE_DIR=/config/captures
fi
if bashio::config.has_value 'api_token'; then
    export QUASAR_API_TOKEN="$(bashio::config 'api_token')"
fi
if [ ! -s /data/api_token ] && [ -z "${QUASAR_API_TOKEN:-}" ]; then
    bashio::log.info "A new API token will be generated in /data/api_token"
fi
cd /data || exit 1
exec python3 -m quasar_adapter serve -c /data/config.toml
