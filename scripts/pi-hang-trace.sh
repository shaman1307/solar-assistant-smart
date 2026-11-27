#!/bin/bash
# Write a local trace of the running Pi. The file stays on disk after power-off.
set -u
export TZ=Europe/Warsaw
dir=/var/log/pi-hang
mkdir -p "$dir"
chmod 755 "$dir"
out=$(mktemp)
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
  uptime
  free -m | awk 'NR==1 || NR==2'
  echo "-- smart --"
  systemctl show smart -p ActiveState -p SubState -p MainPID -p NRestarts --no-pager
  pid=$(systemctl show -p MainPID --value smart 2>/dev/null || true)
  if [ -n "${pid}" ] && [ "${pid}" != "0" ] && [ -d "/proc/${pid}" ]; then
    echo "wchan $(cat "/proc/${pid}/wchan" 2>/dev/null || true)"
    echo "threads:"
    n=0
    for t in /proc/${pid}/task/*; do
      tid=$(basename "$t")
      echo "  ${tid} $(cat "$t/wchan" 2>/dev/null || true)"
      n=$((n + 1))
      if [ "$n" -ge 40 ]; then
        break
      fi
    done
  fi
  echo "-- kernel hung-task timeout --"
  cat /proc/sys/kernel/hung_task_timeout_secs 2>/dev/null || true
  echo "-- dmesg --"
  dmesg -T 2>/dev/null | tail -n 15 || true
  day=$(date +%F)
  log="/home/solar-assistant/project/logs/$(date +%Y)/$(date +%m)/${day}.log"
  if [ -f "$log" ]; then
    echo "-- app log --"
    tail -n 6 "$log" || true
  fi
} > "$out" 2>&1
week=$(date +%G-W%V)
if [ ! -f "$dir/week" ] || [ "$(cat "$dir/week" 2>/dev/null)" != "$week" ]; then
  : > "$dir/trace.log"
  echo "$week" > "$dir/week"
  chmod 644 "$dir/week"
fi
cat "$out" >> "$dir/trace.log"
cp "$out" "$dir/latest.txt"
chmod 644 "$dir/trace.log" "$dir/latest.txt"
rm -f "$out"
