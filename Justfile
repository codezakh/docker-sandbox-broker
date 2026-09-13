set positional-arguments

# Start in the foreground (e.g. in tmux), with all output sent to rotating files.
# Override LOG_FILE, LOG_MAX_BYTES, or LOG_BACKUPS via environment variables.
start *args:
    #!/usr/bin/env bash
    set -euo pipefail
    log_file="${LOG_FILE:-/var/tmp/docker-sandbox-broker-$(id -u)/logs/broker.log}"
    printf 'Broker logs: %s (rotation enabled)\n' "$log_file"
    exec python3 scripts/rotate_output.py --log-file "$log_file" \
        --max-bytes "${LOG_MAX_BYTES:-10485760}" --backups "${LOG_BACKUPS:-5}" \
        -- uv run docker-sandbox-broker-host "$@"
