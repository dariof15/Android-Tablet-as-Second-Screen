#!/usr/bin/env python3
"""Low-latency second screen for the tablet: virtual monitor -> MJPEG -> browser.

Why MJPEG instead of the RDP path
---------------------------------
GNOME Remote Desktop on this machine can only send RemoteFX: there is no H.264
encoder anywhere in the stack (grd is NVENC-only, and Ubuntu's FreeRDP is built
with WITH_GFX_H264=OFF). RemoteFX is *progressive* - it sends a coarse tile and
refines it afterwards - which is what shows up as artifacts appearing and then
disappearing under the cursor.

MJPEG has no inter-frame state at all. Every frame is complete and independent,
so there is nothing to refine and nothing to flicker. PipeWire only produces
frames when the screen actually changes, so a static desktop costs close to
nothing.

Colour fidelity
---------------
Two things were measured against a lossless reference and both matter:

1. colorimetry=1:4:7:1 is not optional. Without it GStreamer encodes
   limited-range (16-235) YUV while JPEG is defined as full-range, so the
   decoder stretches it wrongly: blacks lifted, whites clipped at ~248, mean
   channel error ~17. Forcing full range takes that to 0.45.

2. 4:4:4 beats the GPU. vaapijpegenc cannot encode Y444 on this driver
   ("Internal data stream error"), only 4:2:2/4:2:0, which loses colour detail
   and hits max channel errors of 255 on sharp edges. Software jpegenc with
   Y444 measures mean error 0.45 / max 5 for about 4 KB more per frame.

So software Y444 is the default. It encodes at ~163 fps at 1536x1024
(~12 ms of CPU per frame), which is 5x the headroom needed for 30 fps.
Use --hw for the iGPU path to spend colour accuracy instead of CPU:
~408 fps and ~3.7 ms per frame, but mean error 7.3.

On-demand lifecycle
-------------------
The virtual monitor is created when a browser actually starts watching and
destroyed a few seconds after the last one stops. Holding it open permanently
would leave GNOME with a monitor nobody can see, and windows would wander onto
it. So this can run as an always-on service safely: the screen appears when you
open the page and goes away when you close it.

This mode is display-only. Touch/keyboard would need input injection through
org.gnome.Mutter.RemoteDesktop, which is deliberately not done here.

Configuration comes from .env (see .env.example); a command-line flag overrides
it, and a real environment variable overrides the file.

Usage:
    ./mjpeg-screen.py                          # everything from .env
    ./mjpeg-screen.py -W 1920 -H 1280 --fps 40 # override for one run
    ./mjpeg-screen.py --encoder hw             # iGPU encoder, worse colour

Then on the tablet, browse to http://127.0.0.1:<HTTP_PORT> (the adb reverse
tunnel is set up by usb-tunnel.sh).
"""

import argparse
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dbus
import dbus.mainloop.glib
import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

import settings  # noqa: E402

BOUNDARY = "tabletframe"

PAGE = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Second screen</title>
<style>
  html,body{margin:0;height:100%;background:#000;overflow:hidden}
  /* object-fit:contain keeps the desktop's aspect ratio without cropping it. */
  img{display:block;width:100vw;height:100vh;object-fit:contain}
  #hint{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);
        color:#888;font:600 4vw system-ui,sans-serif;text-align:center}
</style>
<div id="hint">connecting&hellip;</div>
<img id="v" alt="">
<script>
  const img = document.getElementById('v'), hint = document.getElementById('hint');
  img.onload = () => hint.remove();
  // Cache-bust so a reconnect doesn't latch onto the previous dead stream.
  img.src = '/stream?t=' + Date.now();
  // Tapping goes fullscreen; browser chrome otherwise eats screen area.
  document.body.addEventListener('click', () => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen?.();
  });
</script>
"""


class LatestFrame:
    """Holds only the newest frame.

    Never queue frames: a queue turns a slow client into growing latency, which
    is the whole thing we are trying to avoid. A client that falls behind skips
    frames instead.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._data = None
        self._seq = 0
        self._down = False

    def publish(self, data):
        with self._cond:
            self._data = data
            self._seq += 1
            self._cond.notify_all()

    def wait_newer(self, seen, timeout=4.0):
        """Block until a frame newer than `seen` exists.

        On timeout the current frame is returned again, which doubles as a
        keep-alive so a static desktop doesn't look like a stalled connection.
        """
        with self._cond:
            if self._seq == seen and not self._down:
                self._cond.wait(timeout)
            if self._down or self._data is None:
                return None, self._seq
            return self._data, self._seq

    @property
    def down(self):
        return self._down

    def shutdown(self):
        with self._cond:
            self._down = True
            self._cond.notify_all()


class Stats:
    def __init__(self):
        self.frames = 0
        self.bytes = 0
        self.started = time.monotonic()


FRAME = LatestFrame()
STATS = Stats()


class VirtualScreen:
    """A Mutter virtual monitor plus the pipeline that encodes it to JPEG."""

    # JPEG is a full-range format; without this the decoder misreads the levels.
    FULL_RANGE = "colorimetry=1:4:7:1"

    def __init__(self, args):
        self.args = args
        self.pipeline = None
        self.session = None
        self.node_id = None
        self.using_hw = args.encoder == "hw"

        bus = dbus.SessionBus()
        sc = dbus.Interface(
            bus.get_object("org.gnome.Mutter.ScreenCast",
                           "/org/gnome/Mutter/ScreenCast"),
            "org.gnome.Mutter.ScreenCast")
        self.session = dbus.Interface(
            bus.get_object("org.gnome.Mutter.ScreenCast", sc.CreateSession({})),
            "org.gnome.Mutter.ScreenCast.Session")
        try:
            # is-platform=True is what makes Mutter treat this as a real monitor
            # that extends the desktop, rather than an invisible capture surface.
            stream_path = self.session.RecordVirtual({
                "is-platform": dbus.Boolean(True),
                "cursor-mode": dbus.UInt32(1),   # draw the cursor into frames
            })
            self.stream = bus.get_object("org.gnome.Mutter.ScreenCast",
                                         stream_path)
            self.stream.connect_to_signal(
                "PipeWireStreamAdded", self.on_stream,
                dbus_interface="org.gnome.Mutter.ScreenCast.Stream")
            self.session.Start()
        except Exception:
            # Don't leak a half-built Mutter session (and its virtual monitor)
            # if anything past CreateSession fails.
            self.close()
            raise

    def encoder_chain(self, use_hw):
        q = self.args.quality
        if use_hw:
            # The GPU cannot do Y444 here, so 4:2:2 is the best it offers.
            return (f"vaapipostproc ! "
                    f"video/x-raw(memory:VASurface),format=YUY2,{self.FULL_RANGE} ! "
                    f"vaapijpegenc quality={q}")
        return (f"videoconvert ! video/x-raw,format=Y444,{self.FULL_RANGE} ! "
                f"jpegenc quality={q}")

    def on_stream(self, node_id):
        self.node_id = node_id
        a = self.args
        print(f"virtual monitor up: {a.width}x{a.height}@{a.fps} (node {node_id})",
              flush=True)
        self.build(self.args.encoder == "hw")

    def build(self, use_hw):
        a = self.args
        # The capsfilter right after pipewiresrc is what sets the virtual
        # monitor's resolution - Mutter sizes it from what the consumer asks for.
        desc = (f"pipewiresrc path={self.node_id} ! "
                f"video/x-raw,max-framerate={a.fps}/1,width={a.width},height={a.height} ! "
                f"{self.encoder_chain(use_hw)} ! "
                f"appsink name=out emit-signals=true max-buffers=1 drop=true sync=false")
        self.using_hw = use_hw
        print(f"encoder: {'iGPU 4:2:2' if use_hw else 'software 4:4:4'} "
              f"quality={a.quality}", flush=True)
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as e:
            print(f"pipeline build failed: {e.message}", file=sys.stderr, flush=True)
            return
        self.pipeline.get_by_name("out").connect("new-sample", self.on_sample)
        gbus = self.pipeline.get_bus()
        gbus.add_signal_watch()
        gbus.connect("message::error", self.on_error)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            print("pipeline refused to start", file=sys.stderr, flush=True)

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                FRAME.publish(bytes(info.data))
                STATS.frames += 1
                STATS.bytes += info.size
            finally:
                buf.unmap(info)
        return Gst.FlowReturn.OK

    def on_error(self, _bus, msg):
        err, _dbg = msg.parse_error()
        print(f"gstreamer error: {err.message}", file=sys.stderr, flush=True)
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        # The virtual monitor outlives the pipeline, so a rebuild is enough.
        if self.using_hw:
            print("falling back to the software encoder", flush=True)
            self.build(use_hw=False)

    def close(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.session:
            try:
                self.session.Stop()
            except Exception:
                pass
            self.session = None
            print("virtual monitor removed", flush=True)


class ScreenManager:
    """Brings the virtual monitor up only while somebody is watching.

    HTTP handlers run on their own threads, but D-Bus and GStreamer want the
    GLib loop thread. Rather than marshal individual calls across threads, the
    handlers just move a counter and a reconcile tick on the GLib loop makes the
    state match it. Fewer moving parts, no cross-thread source juggling.
    """

    def __init__(self, args, grace=8.0):
        self.args = args
        self.grace = grace
        self.lock = threading.Lock()
        self.clients = 0
        self.screen = None
        self.idle_since = None

    def add_client(self):
        with self.lock:
            self.clients += 1
            return self.clients

    def remove_client(self):
        with self.lock:
            self.clients -= 1
            return self.clients

    def count(self):
        with self.lock:
            return self.clients

    def reconcile(self):
        watching = self.count()
        if watching > 0:
            self.idle_since = None
            if self.screen is None:
                print(f"{watching} viewer(s) - starting", flush=True)
                try:
                    self.screen = VirtualScreen(self.args)
                except Exception as e:
                    print(f"could not create virtual monitor: {e}",
                          file=sys.stderr, flush=True)
        elif self.screen is not None:
            now = time.monotonic()
            if self.idle_since is None:
                self.idle_since = now
            elif now - self.idle_since >= self.grace:
                print("no viewers - stopping", flush=True)
                self.screen.close()
                self.screen = None
                self.idle_since = None
        return True  # keep the timeout installed

    def close(self):
        if self.screen:
            self.screen.close()
            self.screen = None


MANAGER = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # keep the journal readable

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_bytes(PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/stream":
            self.stream()
        elif path == "/stats":
            up = time.monotonic() - STATS.started
            live = MANAGER.screen is not None
            self.send_bytes(
                (f"viewers={MANAGER.count()} monitor={'up' if live else 'down'} "
                 f"frames={STATS.frames} avg_fps={STATS.frames/up:.1f} "
                 f"MB={STATS.bytes/1e6:.1f}\n").encode(), "text/plain")
        else:
            self.send_error(404)

    def send_bytes(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        MANAGER.add_client()
        seen = 0
        # The monitor is created by the reconcile tick, so allow it time to
        # appear before giving up on ever seeing a frame.
        deadline = time.monotonic() + 15.0
        try:
            while True:
                data, seen = FRAME.wait_newer(seen)
                if data is None:
                    # wait_newer returns immediately once shut down, so bail out
                    # rather than spinning on it.
                    if FRAME.down or time.monotonic() > deadline:
                        break
                    time.sleep(0.05)
                    continue
                deadline = time.monotonic() + 15.0
                self.wfile.write(
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n".encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            MANAGER.remove_client()


def main():
    global MANAGER
    p = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="Defaults come from .env; see .env.example.")
    p.add_argument("-W", "--width", type=int,
                   default=settings.get_int("SCREEN_WIDTH", 1536))
    p.add_argument("-H", "--height", type=int,
                   default=settings.get_int("SCREEN_HEIGHT", 1024))
    p.add_argument("--fps", type=int,
                   default=settings.get_int("SCREEN_FPS", 30))
    p.add_argument("-q", "--quality", type=int,
                   default=settings.get_int("JPEG_QUALITY", 95),
                   help="JPEG quality 0-100")
    p.add_argument("-p", "--port", type=int,
                   default=settings.get_int("HTTP_PORT", 8099))
    p.add_argument("--bind", default=settings.get_str("HTTP_BIND", "127.0.0.1"),
                   help="address to listen on; keep 127.0.0.1 unless you "
                        "understand the exposure (the stream is unencrypted)")
    p.add_argument("--encoder", choices=("software", "hw"),
                   default=settings.get_str("ENCODER", "software"),
                   help="software = 4:4:4, best colour; hw = iGPU 4:2:2, "
                        "cheaper CPU but measurably worse colour")
    p.add_argument("--grace", type=float,
                   default=settings.get_float("IDLE_GRACE", 8.0),
                   help="seconds to keep the monitor after the last viewer "
                        "leaves, so a page reload doesn't drop it")
    args = p.parse_args()

    # argparse's `choices` and type= only validate values passed on the command
    # line - a bad value coming from .env sails straight through as the default.
    # Catch those here so a typo fails loudly instead of silently degrading.
    if args.encoder not in ("software", "hw"):
        p.error(f"ENCODER must be 'software' or 'hw', got {args.encoder!r}")
    if not 0 <= args.quality <= 100:
        p.error(f"JPEG_QUALITY must be 0-100, got {args.quality}")
    for name, value in (("SCREEN_WIDTH", args.width),
                        ("SCREEN_HEIGHT", args.height),
                        ("SCREEN_FPS", args.fps)):
        if value <= 0:
            p.error(f"{name} must be positive, got {value}")
    if not 1 <= args.port <= 65535:
        p.error(f"HTTP_PORT must be 1-65535, got {args.port}")
    if args.grace < 0:
        p.error(f"IDLE_GRACE must not be negative, got {args.grace}")

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    Gst.init(None)

    MANAGER = ScreenManager(args, grace=args.grace)
    GLib.timeout_add(1000, MANAGER.reconcile)

    loop = GLib.MainLoop()
    threading.Thread(target=loop.run, daemon=True).start()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: listening on {args.bind} - the MJPEG stream is "
              f"unencrypted and unauthenticated", file=sys.stderr, flush=True)

    def shutdown(*_):
        print("shutting down", flush=True)
        FRAME.shutdown()
        MANAGER.close()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"waiting for viewers on http://{args.bind}:{args.port}  "
          f"({args.width}x{args.height}@{args.fps} q{args.quality} "
          f"{args.encoder}; monitor is created on demand)", flush=True)
    try:
        server.serve_forever()
    finally:
        MANAGER.close()


if __name__ == "__main__":
    main()
