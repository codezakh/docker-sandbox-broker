#!/usr/bin/env bash
set -euo pipefail
mkdir -p /logs/verifier
if [ "$(cat /workspace/answer.txt 2>/dev/null)" = "compose-sidecar-ok" ]; then
    printf 1 > /logs/verifier/reward.txt
    exit 0
fi
printf 0 > /logs/verifier/reward.txt
exit 1
