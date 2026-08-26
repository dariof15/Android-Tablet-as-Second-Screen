#!/usr/bin/env bash
# Measure what the encoder is costing, so settings changes can be compared on
# numbers instead of impressions.
#
# JPEG encoding is per-frame and largely single-threaded, so the figure that
# matters is "% of ONE core". Frames are only produced when the screen changes,
# which means this measurement depends entirely on what is happening on the
# tablet screen at the time.
#
#   IMPORTANT: for a fair before/after comparison, do the SAME thing during each
#   run - e.g. scroll a long page steadily, or leave it completely idle. Samples
#   taken during different activity are not comparable.
#
# Usage:  ./bench.sh [seconds]        (default 15)

set -uo pipefail

SECS="${1:-15}"
HZ="$(getconf CLK_TCK)"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PORT="${HTTP_PORT:-$(grep -E '^HTTP_PORT=' "${SCRIPT_DIR}/.env" 2>/dev/null | cut -d= -f2)}"
PORT="${PORT:-8099}"

cpu_ticks() { awk '{print $14+$15}' "/proc/$1/stat" 2>/dev/null; }

pid="$(systemctl --user show -p MainPID --value tablet-screen.service 2>/dev/null)"
if [ -z "$pid" ] || [ "$pid" = "0" ]; then
	for p in $(pgrep -x python3 2>/dev/null); do
		if grep -qa mjpeg-screen "/proc/$p/cmdline" 2>/dev/null; then
			pid="$p"
			break
		fi
	done
fi
if [ -z "$pid" ] || [ "$pid" = "0" ]; then
	echo "mjpeg-screen.py is not running." >&2
	exit 1
fi

echo "== state =="
curl -s --max-time 2 "http://127.0.0.1:${PORT}/stats" | sed 's/^/  /' ||
	echo "  (could not reach /stats on port ${PORT})"
python3 - <<'PY'
import dbus
try:
    dc = dbus.SessionBus().get_object(
        'org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig')
    _, mons, _logical, _ = dc.GetCurrentState(
        dbus_interface='org.gnome.Mutter.DisplayConfig')
except Exception as e:
    print(f"  (could not query Mutter: {e})")
    raise SystemExit(0)
for spec, modes, _p in mons:
    if not str(spec[0]).startswith('Meta'):
        continue
    for m in modes:
        if m[6].get('is-current'):
            print(f"  virtual monitor: {m[1]}x{m[2]} "
                  f"({int(m[1])*int(m[2])/1e6:.2f} Mpixel)")
PY

echo
echo "== sampling ${SECS}s (% of one core) =="
shell_pid="$(pgrep -x gnome-shell | head -1)"
s_start="$(cpu_ticks "$shell_pid")"
peak=0 total=0 samples=0

for _ in $(seq 1 "$SECS"); do
	a="$(cpu_ticks "$pid")"
	sleep 1
	b="$(cpu_ticks "$pid")"
	if [ -z "$a" ] || [ -z "$b" ]; then continue; fi
	pct=$(((b - a) * 100 / HZ))
	total=$((total + pct))
	samples=$((samples + 1))
	[ "$pct" -gt "$peak" ] && peak=$pct
	printf '  %3d%%\n' "$pct"
done

s_end="$(cpu_ticks "$shell_pid")"

echo
echo "== result =="
if [ "$samples" -gt 0 ]; then
	echo "  mjpeg-screen : avg $((total / samples))%  peak ${peak}%  of one core"
fi
if [ -n "${s_start:-}" ] && [ -n "${s_end:-}" ]; then
	echo "  gnome-shell  : avg $(((s_end - s_start) * 100 / (HZ * SECS)))%  of one core"
fi
echo "  (100% of one core = a saturated encoder thread = dropped frames)"
