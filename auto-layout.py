#!/usr/bin/env python3
"""Pin the tablet's virtual monitor to a fixed spot in the display layout.

Why this exists
---------------
Mutter stamps every virtual monitor it creates with a fresh serial - 0x000003,
0x000004, 0x000005, ... - so from GNOME's point of view each RDP connection
brings a monitor it has never seen before. Nothing in ~/.config/monitors.xml
matches, so Mutter falls back to a default arrangement instead of the one you
set last time. That is why the desktop doesn't extend onto the tablet until you
open Settings > Displays and move it by hand, every single time.

This daemon watches Mutter's MonitorsChanged signal and applies the wanted
arrangement itself as soon as the virtual monitor shows up.

Configuration comes from .env (see .env.example): MONITOR_SIDE, MONITOR_SCALE,
BUILTIN_CONNECTOR and LAYOUT_PERSIST.

On LAYOUT_PERSIST: applying a *persistent* config makes Mutter append another
entry to monitors.xml for a serial that will never recur, so the file grows by
one dead stanza per connection. The default is a temporary apply, which arranges
the screen without littering the file.
"""

import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

import settings  # noqa: E402

VIRTUAL_PREFIX = "Meta-"

BUILTIN_PREFIX = settings.get_str("BUILTIN_CONNECTOR", "eDP-")
SIDE = settings.get_str("MONITOR_SIDE", "left").lower()
SCALE = settings.get_float("MONITOR_SCALE", 1.0)
PERSIST = settings.get_bool("LAYOUT_PERSIST", False)

# Mutter's MetaMonitorsConfigMethod
METHOD_VERIFY, METHOD_TEMPORARY, METHOD_PERSISTENT = 0, 1, 2
METHOD = METHOD_PERSISTENT if PERSIST else METHOD_TEMPORARY

CONFIG_SIGNATURE = "(uua(iiduba(ssa{sv}))a{sv})"


def log(msg):
    print(msg, flush=True)


class LayoutPinner:
    def __init__(self):
        self.proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,
            "org.gnome.Mutter.DisplayConfig",
            "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig",
            None,
        )
        # Guards against reacting to the MonitorsChanged that our own apply emits.
        self.applying = False
        self.pending = None
        self.warned_builtin = False

    def get_state(self):
        res = self.proxy.call_sync(
            "GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None
        )
        return res.unpack()

    @staticmethod
    def pick_mode(modes):
        """Return (mode_id, width, height) for the current, else preferred, mode."""
        current = preferred = None
        for mode in modes:
            mode_id, width, height = mode[0], mode[1], mode[2]
            props = mode[6] if len(mode) > 6 else {}
            if props.get("is-current"):
                current = (mode_id, width, height)
            if props.get("is-preferred"):
                preferred = (mode_id, width, height)
        if current:
            return current
        if preferred:
            return preferred
        first = modes[0]
        return (first[0], first[1], first[2])

    def desired_layout(self, monitors):
        """Build the logical-monitor list, or None if there's nothing to do."""
        virtual = builtin = None
        for spec, modes, _props in monitors:
            connector = spec[0]
            if connector.startswith(VIRTUAL_PREFIX):
                virtual = (connector, modes)
            elif connector.startswith(BUILTIN_PREFIX):
                builtin = (connector, modes)

        if virtual is None:
            return None
        if builtin is None:
            # Almost always a wrong BUILTIN_CONNECTOR in .env, which would
            # otherwise fail silently and look like the daemon doing nothing.
            if not self.warned_builtin:
                self.warned_builtin = True
                names = ", ".join(str(s[0][0]) for s in monitors)
                log(f"no monitor matching BUILTIN_CONNECTOR={BUILTIN_PREFIX!r}; "
                    f"connectors present: {names}")
            return None

        v_mode, v_w, v_h = self.pick_mode(virtual[1])
        b_mode, b_w, b_h = self.pick_mode(builtin[1])

        # Logical size is physical size divided by the scale factor.
        v_logical_w = int(round(v_w / SCALE))
        b_logical_w = int(round(b_w / 1.0))

        if SIDE == "right":
            v_x, b_x = b_logical_w, 0
        else:
            v_x, b_x = 0, v_logical_w

        # (x, y, scale, transform, primary, [(connector, mode_id, {})])
        virtual_lm = (v_x, 0, SCALE, 0, False, [(virtual[0], v_mode, {})])
        builtin_lm = (b_x, 0, 1.0, 0, True, [(builtin[0], b_mode, {})])
        return [virtual_lm, builtin_lm]

    @staticmethod
    def current_layout(logical_monitors):
        """Normalise Mutter's current layout for comparison with ours."""
        out = []
        for x, y, scale, transform, primary, monitors, _props in logical_monitors:
            out.append((x, y, round(scale, 3), transform, primary,
                        [m[0] for m in monitors]))
        return sorted(out, key=lambda e: e[5])

    @staticmethod
    def normalise_desired(desired):
        out = []
        for x, y, scale, transform, primary, monitors in desired:
            out.append((x, y, round(scale, 3), transform, primary,
                        [m[0] for m in monitors]))
        return sorted(out, key=lambda e: e[5])

    def apply(self):
        self.pending = None
        try:
            serial, monitors, logical, _props = self.get_state()
        except GLib.Error as e:
            log(f"could not read monitor state: {e.message}")
            return False

        desired = self.desired_layout(monitors)
        if desired is None:
            return False

        if self.normalise_desired(desired) == self.current_layout(logical):
            log("layout already correct; nothing to do")
            return False

        names = [m[0][0] for m in monitors]
        log(f"virtual monitor present ({', '.join(names)}); applying layout "
            f"(virtual on the {SIDE}, scale {SCALE:g})")

        args = GLib.Variant(CONFIG_SIGNATURE, (serial, METHOD, desired, {}))
        try:
            self.applying = True
            self.proxy.call_sync(
                "ApplyMonitorsConfig", args, Gio.DBusCallFlags.NONE, -1, None
            )
            log("layout applied")
        except GLib.Error as e:
            log(f"ApplyMonitorsConfig failed: {e.message}")
        finally:
            # Release the guard after the resulting signal has been delivered.
            GLib.timeout_add(700, self._clear_guard)
        return False

    def _clear_guard(self):
        self.applying = False
        return False

    def on_signal(self, _proxy, _sender, signal, _params):
        if signal != "MonitorsChanged" or self.applying:
            return
        # Mutter emits several changes as a monitor comes up; coalesce them.
        if self.pending:
            GLib.source_remove(self.pending)
        self.pending = GLib.timeout_add(400, self.apply)

    def run(self):
        self.proxy.connect("g-signal", self.on_signal)
        log(f"watching for virtual monitors (side={SIDE}, scale={SCALE:g}, "
            f"method={'persistent' if PERSIST else 'temporary'})")
        self.apply()  # handle a monitor that is already connected
        GLib.MainLoop().run()


if __name__ == "__main__":
    if SIDE not in ("left", "right"):
        sys.exit(f"SECOND_SCREEN_SIDE must be 'left' or 'right', got {SIDE!r}")
    try:
        LayoutPinner().run()
    except KeyboardInterrupt:
        pass
