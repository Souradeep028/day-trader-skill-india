#!/usr/bin/env bash
# trader-broker's day runner: the paper autopilot for one stretch of the trading day, where cron is not an option
# (a Claude Code cloud session, a laptop terminal). Run it from the trading folder:
#   cd ~/trading && /path/to/trader-broker/day.sh 11:00
# It runs the pre-flight if today's is missing, waits for the first bar (9:16), then a paper pass at minute
# 1, 6, 11, ... of every hour (a minute after each 5-minute bar closes, as in the README's cron) until the time
# given (India time), and exits. Only one runs at a time (trader/state/day.lock). The autopilot itself exits at
# once outside 9:15 to 15:30 and on holidays, and trades paper only.
set -u
CODE=${CODE:-$(cd "$(dirname "$0")/.." && pwd)}
UNTIL=${1:?until HH:MM (IST)}
LOG=trader/autopilot.log
now_ist() { TZ=Asia/Kolkata date +%H:%M; }
mkdir -p trader/state
exec 9>trader/state/day.lock
if ! flock -n 9; then echo "day.sh: another stretch is already running; not starting a second one"; exit 0; fi
if [ ! -f trader/state/preflight.json ] || ! grep -q "\"date\": \"$(TZ=Asia/Kolkata date +%F)\"" trader/state/preflight.json; then
  python3 "$CODE/trader-broker/preflight.py" >> "$LOG" 2>&1
fi
# no passes before the first 5-minute bar has closed (a pass before the open still counts in the heartbeat)
while [ "$(now_ist)" \< "09:16" ] && [ "$(now_ist)" \< "$UNTIL" ]; do sleep 20; done
while [ "$(now_ist)" \< "$UNTIL" ]; do
  # sleep to the next minute that is 1 past a multiple of 5 (09:16, 09:21, ...), at second 5
  s=$(TZ=Asia/Kolkata date +%s); m=$(( (s / 60) % 5 )); sec=$(( s % 60 ))
  wait=$(( ((6 - m) % 5) * 60 - sec + 5 ))
  [ "$wait" -le 0 ] && wait=$(( wait + 300 ))
  sleep "$wait"
  [ "$(now_ist)" \< "$UNTIL" ] || break
  echo "== $(TZ=Asia/Kolkata date '+%F %T') IST" >> "$LOG"
  python3 "$CODE/trader-broker/autopilot.py" run --once >> "$LOG" 2>&1
done
echo "day.sh: stopped at $(now_ist) IST (until $UNTIL)"
tail -n 4 "$LOG"
