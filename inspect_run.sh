#!/usr/bin/env bash
# Jenkins only enqueues a fixed, validated run. Host worker does bounded HTTP I/O.
set -euo pipefail
export TZ=Asia/Shanghai
SPOOL=/opt/inspect-platform/data/spool
case "${BUILD_NUMBER:-}" in ''|*[!0-9]*) echo 'Invalid Jenkins build number' >&2; exit 2;; esac
case "${TRIGGER:-jenkins}" in manual|schedule|jenkins) ;; *) echo 'Invalid trigger' >&2; exit 2;; esac
mkdir -p "$SPOOL"
run_id="$(date +%Y%m%d-%H%M%S)-b${BUILD_NUMBER}"
request="$SPOOL/$run_id.req"
result="$SPOOL/$run_id.result"
umask 077
printf '{"build":"%s","trigger":"%s"}\n' "$BUILD_NUMBER" "${TRIGGER:-jenkins}" > "$request.tmp"
mv "$request.tmp" "$request"
echo "INSPECT_QUEUED=$run_id"
for ((i=0;i<300;i++)); do
  if [ -f "$result" ]; then
    cat "$result"
    if grep -qx 'exit_code=0' "$result"; then exit 0; else exit 1; fi
  fi
  sleep 1
done
echo 'Inspector worker did not complete within 300 seconds' >&2
exit 2
