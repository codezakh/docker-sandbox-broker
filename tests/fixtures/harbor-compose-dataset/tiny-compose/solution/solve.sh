#!/usr/bin/env bash
set -euo pipefail
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    if wget -qO /workspace/answer.txt http://sidecar:8080/status; then
        exit 0
    fi
    sleep 1
done
exit 1
