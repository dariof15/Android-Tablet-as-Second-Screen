#!/usr/bin/env bash
# Keeps the USB link usable across replugs.
#
# `adb reverse` mappings live inside the adb device connection, so they are lost
# every time the cable is pulled. This loop re-establishes the mapping (and the
# tablet's stay-awake flag) whenever the tablet appears, then blocks until it
# disconnects.
#
# The reverse tunnel makes 127.0.0.1:<HTTP_PORT> *on the tablet* reach the same
# port on this PC, so nothing is ever exposed to the network.

set -uo pipefail

# Load .env from next to this script, without clobbering real env vars.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/.env" ]; then
	while IFS= read -r line || [ -n "$line" ]; do
		# Strip leading whitespace before deciding whether it's a comment,
		# otherwise an indented comment gets parsed as a key.
		line="${line#"${line%%[![:space:]]*}"}"
		case "$line" in '' | '#'*) continue ;; esac
		key="${line%%=*}"
		[ "$key" = "$line" ] && continue
		key="${key%"${key##*[![:space:]]}"}"
		# Only well-formed shell identifiers; anything else would break the
		# indirect expansion below (a comment containing '=', say).
		case "$key" in
		[A-Za-z_]*) ;;
		*) continue ;;
		esac
		case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
		# A real environment variable wins over the file.
		if [ -z "${!key:-}" ]; then
			value="${line#*=}"
			value="$(printf '%s' "$value" |
				sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
					-e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")"
			export "$key=$value"
		fi
	done <"${SCRIPT_DIR}/.env"
fi

PORT="${HTTP_PORT:-8099}"
KEEP_AWAKE="${KEEP_AWAKE:-1}"
SERIAL="${ADB_SERIAL:-}"

log() { printf '%s\n' "$*"; }

# Restrict every adb call to one device when ADB_SERIAL is set.
adb_dev() {
	if [ -n "$SERIAL" ]; then adb -s "$SERIAL" "$@"; else adb "$@"; fi
}

first_device() {
	if [ -n "$SERIAL" ]; then
		adb devices | awk -v s="$SERIAL" '$1==s && $2=="device"{print $1}'
	else
		adb devices | sed -n '2,$p' | awk '$2=="device"{print $1; exit}'
	fi
}

while true; do
	log "waiting for tablet over USB..."
	if ! adb_dev wait-for-usb-device; then
		log "adb wait-for-usb-device failed; retrying in 5s"
		sleep 5
		continue
	fi

	# wait-for-usb-device returns on state 'device', but re-read the serial in
	# case several are attached, and guard against it vanishing again.
	serial="$(first_device)"
	if [ -z "$serial" ]; then
		sleep 2
		continue
	fi
	log "device ${serial} connected"

	# Idempotent: drop any stale mapping first so a reconnect is always clean.
	adb -s "$serial" reverse --remove "tcp:${PORT}" >/dev/null 2>&1 || true
	if adb -s "$serial" reverse "tcp:${PORT}" "tcp:${PORT}" >/dev/null 2>&1; then
		log "reverse tunnel ready: tablet 127.0.0.1:${PORT} -> PC :${PORT}"
	else
		log "WARNING: could not set up reverse tunnel for port ${PORT}"
	fi

	if [ "$KEEP_AWAKE" = "1" ]; then
		# 'true' (all power sources), NOT 'usb': over USB-C PD many tablets
		# report themselves as AC-powered ("AC powered: true / USB powered:
		# false"), so the USB-only bitmask never matches and the screen still
		# sleeps.
		if adb -s "$serial" shell svc power stayon true >/dev/null 2>&1; then
			log "stay-awake enabled (all power sources)"
		fi
	fi

	adb -s "$serial" wait-for-usb-disconnect
	log "device ${serial} disconnected"
done
