#!/bin/bash

# BidClub Daily Podcast Digest -- one-shot run for cron.
#
# 08:00 Beijing time, server in Asia/Shanghai:
#   0 8 * * * cd $YOUR_DIR/bidclub-digest && bash crontab.sh &
# Server in UTC (08:00 Beijing = 00:00 UTC):
#   0 0 * * * cd $YOUR_DIR/bidclub-digest && bash crontab.sh &
#
# The script resolves "yesterday" in [window] timezone itself, so the host's own
# timezone only decides WHEN the job fires, never WHICH day it reports on.
# Overlapping runs are safe: a lock on bidclub_digest.lock makes the second one
# exit immediately without sending.
#
# The service writes its own rotating bidclub_digest.log via [logging] file, so
# this redirect goes to a SEPARATE file. It only needs to catch what the logger
# cannot: an interpreter-level crash before logging is configured, a missing
# dependency, or the wrong Python. Keep the two files distinct, otherwise every
# line lands twice in the same log while [logging] console stays true.

cd "$(dirname "$0")" || exit 1

# Prefer the shared venv python when it is available, else fall back to python3
# so the job also runs off the production host.
if [ -f $YOUR_DIR/env.sh ]; then
    # shellcheck source=/dev/null
    source $YOUR_DIR/env.sh
fi
PY="${VENV_PYTHON:-python3}"

"$PY" bidclub_digest.py >> bidclub_digest_cron.log 2>&1