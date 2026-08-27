# Tablet as a second screen — GNOME on Wayland, over USB

Turn an Android tablet into a real second monitor for a Linux PC, connected by a
**USB-C cable** — no Wi-Fi, no network exposure, no Android app to install.

The desktop genuinely extends onto the tablet: you drag windows there, and it
appears in *Settings → Displays* like any monitor. The tablet just shows a
fullscreen page in its browser.

The usual X11 recipe for this (`xrandr --newmode` + `x11vnc`) cannot work on
Wayland, where the compositor owns the displays. This uses Mutter's virtual
monitor API instead, and streams it as MJPEG for low latency.

## How it works

```
┌──────────────────── PC · Ubuntu · GNOME on Wayland ─────────────────────┐
│                                                                         │
│   GNOME / Mutter                    mjpeg-screen.py                     │
│  ┌────────────────┐  PipeWire   ┌──────────────────────┐                │
│  │ virtual monitor│────────────►│ JPEG encode, 4:4:4   │                │
│  │    "Meta-0"    │             │ software or iGPU     │                │
│  └───────▲────────┘             └──────────┬───────────┘                │
│          │ created on demand               │                            │
│          │                                 ▼                            │
│   auto-layout.py                  HTTP MJPEG server                     │
│   pins it beside                  127.0.0.1:8099                        │
│   the built-in panel              (localhost only)                      │
│                                          │                              │
└──────────────────────────────────────────┼──────────────────────────────┘
                                           │ adb reverse — TCP over USB
                        ═══════════════════╪═══════════════════
                            WIRED USB-C 3.2 cable · no Wi-Fi
                        ═══════════════════╪═══════════════════
                                           │
┌──────────────────────────────────────────┼──────────────────────────────┐
│  Android tablet                          ▼                              │
│  Browser, fullscreen  ───►  http://127.0.0.1:8099                       │
└─────────────────────────────────────────────────────────────────────────┘
```

Three small services do the work:

| Service | Script | Job |
|---|---|---|
| `tablet-screen` | `mjpeg-screen.py` | Creates the virtual monitor, encodes it, serves MJPEG |
| `tablet-screen-layout` | `auto-layout.py` | Puts the monitor in the right place in the layout |
| `tablet-screen-tunnel` | `usb-tunnel.sh` | Re-creates the USB tunnel on every replug; keeps the tablet awake |

The virtual monitor exists **only while a browser is watching**. Open the page
and the screen appears; close it and it disappears a few seconds later. So
nothing is wasted when the tablet is unplugged, and you never end up with a
monitor you can't see.

## Requirements

- **GNOME on Wayland.** Mutter's `ScreenCast` API (version 4+) is the core of
  this; it will not work on KDE, wlroots compositors, or X11. Developed against
  GNOME 46 / Ubuntu 24.04.
- **Android 5.0+** with USB debugging, and a browser.
- Packages:

  ```bash
  sudo apt install adb python3-gi python3-dbus \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-pipewire gstreamer1.0-vaapi
  ```

  `gstreamer1.0-vaapi` is only needed for `ENCODER=hw`.

Tested on an Intel Iris Xe laptop with a 3200×2136 tablet. No discrete GPU
required.

## Setup

1. **On the tablet, enable USB debugging.**
   *Settings → About tablet →* tap the build/version number 7× to unlock
   *Developer options*, then enable **USB debugging**.

2. **Plug the tablet into the PC** with a USB-C cable and tap **Allow** on the
   debugging prompt. Confirm the PC sees it:

   ```bash
   adb devices        # should list your tablet as "device"
   ```

3. **Install:**

   ```bash
   git clone <this-repo> && cd tablet_secondScreen
   ./install.sh
   ```

   This checks dependencies, creates `.env` from `.env.example`, and enables the
   three user services so they start at login.

4. **Set the resolution** to match your tablet's aspect ratio, in `.env`:

   ```bash
   SCREEN_WIDTH=1536      # 3:2, for a 3200x2136 panel
   SCREEN_HEIGHT=1024
   ```

   Then `systemctl --user restart tablet-screen`.

5. **On the tablet, open `http://127.0.0.1:8099`** in the browser and **tap once
   to go fullscreen**. Bookmark it. A second monitor appears on the PC.

To remove everything: `./install.sh --uninstall`.

## Configuration

All settings live in `.env` (copied from `.env.example`). Precedence is
**command-line flag → environment variable → `.env` → default**, so you can
override one value for a single run without editing the file.

| Variable | Default | Meaning |
|---|---|---|
| `SCREEN_WIDTH` | `1536` | Virtual monitor width in pixels. Match your tablet's aspect ratio to avoid black bars. |
| `SCREEN_HEIGHT` | `1024` | Virtual monitor height. Together with width, the main sharpness/performance dial. |
| `SCREEN_FPS` | `30` | Frame-rate ceiling. Frames are only produced when the screen changes, so this is not a constant cost. |
| `JPEG_QUALITY` | `95` | 0–100. 95 is visually lossless for desktop content; below ~85 text softens. |
| `ENCODER` | `software` | `software` = CPU, 4:4:4 chroma, best colour. `hw` = Intel iGPU, 4:2:2, ~3× cheaper CPU but measurably worse colour. |
| `HTTP_PORT` | `8099` | Port served on the PC and tunnelled to the same port on the tablet. |
| `HTTP_BIND` | `127.0.0.1` | Listen address. **Keep localhost** — the stream is unencrypted and unauthenticated. |
| `IDLE_GRACE` | `8` | Seconds to keep the monitor after the last viewer leaves, so a page reload doesn't rearrange your windows. |
| `MONITOR_SIDE` | `left` | Which side of the built-in screen the tablet sits on: `left` or `right`. |
| `MONITOR_SCALE` | `1` | Scale factor. `2` gives a HiDPI-style desktop at half the logical size. Fractional values need GNOME fractional scaling. |
| `BUILTIN_CONNECTOR` | `eDP-` | Connector prefix of your main panel, used to position the tablet. Laptops are `eDP-`; desktops may need `DP-` or `HDMI-`. |
| `LAYOUT_PERSIST` | `0` | Leave at `0`. Mutter gives each virtual monitor a new serial, so persistent writes add a stanza to `monitors.xml` that can never match again. |
| `ADB_SERIAL` | *(empty)* | Pin to one device when several are plugged in (see `adb devices`). |
| `KEEP_AWAKE` | `1` | Stop the tablet's screen sleeping while plugged in. |

## Everyday use

```bash
systemctl --user status  tablet-screen          # is it running?
journalctl --user -u tablet-screen -f           # live log
curl -s http://127.0.0.1:8099/stats             # viewers, fps, bandwidth
./bench.sh 12                                   # encoder CPU, % of one core
```

Run it by hand with overrides (stop the service first to free the port):

```bash
systemctl --user stop tablet-screen
./mjpeg-screen.py -W 1920 -H 1280 --fps 40 -q 98
```

## Notes and limitations

**Display only.** The tablet shows the screen but does not send input — touches
do nothing. Use your PC's mouse and keyboard. (Injecting input is possible via
`org.gnome.Mutter.RemoteDesktop`, but is not implemented here.)

**Staying awake and bright.** Two separate mechanisms, because Android has two
separate behaviours. `KEEP_AWAKE=1` stops the screen switching *off*
(`svc power stayon true`), but Android also dims the panel to roughly 30%
brightness shortly before its screen-off timeout, and stay-awake does not
prevent that. So the page also holds a [Screen Wake
Lock](https://developer.mozilla.org/en-US/docs/Web/API/Screen_Wake_Lock_API),
which stops both. It needs a secure context, which `http://127.0.0.1` counts as.
If your browser refuses the lock and the screen still dims, raise the tablet's
own timeout instead:

```bash
adb shell settings put system screen_off_timeout 1800000   # 30 minutes
```

**Why not RDP?** GNOME Remote Desktop can extend the desktop and *does* carry
touch input, so it looks like the obvious answer. On hardware without NVENC it
is not: GNOME Remote Desktop only implements hardware H.264 via NVIDIA's NVENC,
and Debian/Ubuntu build FreeRDP with `WITH_GFX_H264=OFF`, so there is no H.264
encoder available at all and it falls back to RemoteFX. RemoteFX is
*progressive* — it sends a coarse tile then refines it — which shows up as
artifacts flickering in and out under the cursor. MJPEG has no inter-frame
state, so that class of artifact cannot occur.

**Colour.** JPEG is a full-range format, so the pipeline forces
`colorimetry=1:4:7:1`. Without it, GStreamer emits limited-range (16–235) YUV
that the browser stretches incorrectly — blacks lifted, whites clipped, visibly
washed out. Software encoding also uses 4:4:4 chroma; the Intel driver cannot
encode JPEG from `Y444`, which is why `ENCODER=hw` is the lower-quality option.

**Bandwidth over quality.** MJPEG sends complete frames, which costs far more
bandwidth than a video codec — irrelevant here, because USB gives hundreds of
MB/s and only changed frames are sent at all.

**Security.** The server binds `127.0.0.1` and reaches the tablet through
`adb reverse`, so the stream never touches a network. Changing `HTTP_BIND`
publishes your screen unencrypted and unauthenticated to anyone who can reach
that address.

**Autostart at boot works.** Verified on a cold boot: all three services were
running half a minute after boot, and simply plugging the tablet in and opening
the browser was enough — no restarts, no manual step. The services are ordered
`After=graphical-session.target` but do not depend on Mutter being ready at that
moment: the stream only talks to Mutter once the tablet asks for a frame, and
the layout watcher retries. `Restart=always` is a backstop. If it ever does not
come up, check:

```bash
systemctl --user status tablet-screen
journalctl --user -u tablet-screen -b     # look for a restart loop
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

- [santiagofdezg/linux-extend-screen](https://github.com/santiagofdezg/linux-extend-screen)
  — the X11 tutorial that inspired this, and the source of the `adb reverse`
  trick for carrying the connection over USB.
- [Mirror Hall](https://gitlab.com/nokun/MirrorHall) — reference for driving
  Mutter's `RecordVirtual` D-Bus API to create a desktop-extending virtual
  monitor.
