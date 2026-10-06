#!/usr/bin/env python3
"""
Audiobus Remote — agent (the computer being shared), GUI edition.

Run this on the computer you want to let someone control. It:
  1. asks the Audiobus server for a fresh 10-character session code,
  2. shows the code in a window for you to share,
  3. when a viewer connects, asks you to Approve or Deny,
  4. once approved, streams the screen and applies the viewer's mouse/keyboard.

Transport: the screen is sent as a WebRTC video stream (VP8, one track per
monitor) directly to the viewer's browser. A WebSocket relayed through the
Cloudflare Worker carries everything else — consent, the monitor list, the
viewer's monitor selection ("want"), the agent-window "blockrects", the viewer's
mouse/keyboard input, and the WebRTC offer/answer/ICE signaling. STUN plus
(when configured) Cloudflare TURN handle NAT traversal.

Consent by design: your screen is not shared until you Approve, a banner is shown
the whole time someone is connected, and closing this window ends the session.
"""

import argparse
import asyncio
import fractions
import json
import os
import queue
import ssl
import sys
import threading
import time
import urllib.request

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

try:
    import websockets
    try:
        from mss import MSS as ScreenGrabber  # mss >= 10
    except ImportError:
        from mss import mss as ScreenGrabber  # mss < 10
    from PIL import Image
    from pynput.mouse import Button, Controller as MouseController
    from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
    import numpy as np
    import av
    from aiortc import (
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
        MediaStreamTrack,
    )
    from aiortc.mediastreams import MediaStreamError
    from aiortc.sdp import candidate_from_sdp
except ImportError as exc:  # pragma: no cover - dependency guard
    try:
        messagebox.showerror(
            "Audiobus Remote",
            f"Missing dependency: {exc.name}\n\n"
            "Install requirements first:\n"
            "    python -m pip install -r requirements.txt",
        )
    except Exception:
        sys.stderr.write(f"Missing dependency: {exc.name}\n")
    sys.exit(1)

# Set to the deployed Worker URL. Override with --server or $AUDIOBUS_SERVER.
DEFAULT_SERVER = "https://audiobus-remote.soft-daf.workers.dev"

# A plain product User-Agent. The default "Python-urllib/..." UA is blocked by
# Cloudflare's bot filtering, so we must send our own on every request.
USER_AGENT = "AudiobusRemoteAgent/0.1"

# --------------------------------------------------------------------------- #
# Localization: show the UI in the user's OS language, falling back to English.
# --------------------------------------------------------------------------- #

try:
    from translations import TRANSLATIONS
except Exception:
    TRANSLATIONS = {"en": {}}

try:
    from logo import LOGO_HEADER_B64, LOGO_ICONS_B64
except Exception:
    LOGO_HEADER_B64 = None
    LOGO_ICONS_B64 = []


def _resource_path(name):
    """Path to a bundled data file, in source runs and in the frozen exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _detect_os_lang():
    """Best-effort OS UI language as a lowercase BCP-47-ish tag (e.g. 'es', 'pt-br')."""
    if sys.platform == "win32":
        try:
            import ctypes
            import locale as _loc

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            name = _loc.windows_locale.get(lcid)  # e.g. "es_ES"
            if name:
                return name.replace("_", "-").lower()
        except Exception:
            pass
    try:
        import locale as _loc

        name = _loc.getdefaultlocale()[0]
        if name:
            return name.replace("_", "-").lower()
    except Exception:
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var)
        if v:
            return v.split(".")[0].split(":")[0].replace("_", "-").lower()
    return "en"


def _pick_lang(detected):
    if detected in TRANSLATIONS:
        return detected
    base = detected.split("-")[0]
    if base in TRANSLATIONS:
        return base
    return "en"


_LANG = _pick_lang(_detect_os_lang())
_DICT = TRANSLATIONS.get(_LANG, TRANSLATIONS.get("en", {}))
_EN = TRANSLATIONS.get("en", {})


def tr(key):
    """Translate a UI string key to the detected language (English fallback)."""
    v = _DICT.get(key)
    if v is None:
        v = _EN.get(key, key)
    return v

# --------------------------------------------------------------------------- #
# Theme: follow the OS light/dark setting (detected once at startup).
# --------------------------------------------------------------------------- #

THEMES = {
    "dark": {
        "BG": "#0b0e14", "CARD": "#131824", "CODEBG": "#05070b", "BORDER": "#263041",
        "TEXT": "#e6ebf2", "MUTED": "#8a97a8", "BRAND": "#5b8cff",
        "GOOD": "#3ecf8e", "BAD": "#ff6b6b", "WARN": "#ffcf5c",
        "APPROVE_FG": "#06231a", "DENY_FG": "#2a0d0d",
        "STOP_BG": "#3a1f26", "STOP_FG": "#ff8585",
        "BANNER_BG": "#1e4636", "BANNER_FG": "#3ecf8e",
        "URL_FG": "#c9d3e0", "COPY_FG": "#ffffff",
        # Orange auto-accept checkbox box (bold, clearly set apart).
        "CHECK_BG": "#9a5214", "CHECK_BORDER": "#f59e0b",
        "CHECK_FG": "#ffffff", "CHECK_SEL": "#4a2a0a",
        # Neutral (secondary / Cancel) button, clearly raised above the card.
        "NEUTRAL_BG": "#39445a", "NEUTRAL_FG": "#e6ebf2",
    },
    "light": {
        "BG": "#eef1f7", "CARD": "#ffffff", "CODEBG": "#eef1f7", "BORDER": "#d2d9e6",
        "TEXT": "#141a24", "MUTED": "#566072", "BRAND": "#234f9e",
        "GOOD": "#157a4f", "BAD": "#c23b3b", "WARN": "#8a6d00",
        "APPROVE_FG": "#ffffff", "DENY_FG": "#ffffff",
        "STOP_BG": "#f3d9dd", "STOP_FG": "#b0323c",
        "BANNER_BG": "#d6f0e2", "BANNER_FG": "#1a7a50",
        "URL_FG": "#3a4556", "COPY_FG": "#ffffff",
        "CHECK_BG": "#fb923c", "CHECK_BORDER": "#c2410c",
        "CHECK_FG": "#3a1703", "CHECK_SEL": "#ffffff",
        "NEUTRAL_BG": "#c9d1e0", "NEUTRAL_FG": "#141a24",
    },
}


def _detect_os_theme():
    """'light' or 'dark' from the Windows personalization setting (default dark)."""
    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if val == 1 else "dark"
        except Exception:
            pass
    return "dark"


# Theme choice persisted across runs: "auto" (follow the OS), "light", or "dark".

def _config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AudiobusRemote", "config.json")


def _load_theme_choice():
    try:
        with open(_config_path(), encoding="utf-8") as f:
            v = json.load(f).get("theme", "auto")
            return v if v in ("auto", "light", "dark") else "auto"
    except Exception:
        return "auto"


def _save_theme_choice(choice):
    try:
        p = _config_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"theme": choice}, f)
    except Exception:
        pass


def _resolve_theme(choice):
    """Map a choice ('auto'/'light'/'dark') to a concrete 'light'/'dark'."""
    return choice if choice in ("light", "dark") else _detect_os_theme()


# Theme color globals, (re)assigned by _apply_theme(). Widgets read these at
# construction, so switching theme = _apply_theme(new) then rebuild the UI.
BG = CARD = CODEBG = BORDER = TEXT = MUTED = BRAND = GOOD = BAD = WARN = ""
APPROVE_FG = DENY_FG = STOP_BG = STOP_FG = BANNER_BG = BANNER_FG = URL_FG = ""
COPY_FG = CHECK_BG = CHECK_BORDER = CHECK_FG = CHECK_SEL = NEUTRAL_BG = NEUTRAL_FG = ""
_THEME = THEMES["dark"]


def _apply_theme(name):
    """Point all theme color globals at THEMES[name] ('light' or 'dark')."""
    global _THEME, BG, CARD, CODEBG, BORDER, TEXT, MUTED, BRAND, GOOD, BAD, WARN
    global APPROVE_FG, DENY_FG, STOP_BG, STOP_FG, BANNER_BG, BANNER_FG, URL_FG, COPY_FG
    global CHECK_BG, CHECK_BORDER, CHECK_FG, CHECK_SEL, NEUTRAL_BG, NEUTRAL_FG
    t = THEMES[name]
    _THEME = t
    BG, CARD, CODEBG, BORDER = t["BG"], t["CARD"], t["CODEBG"], t["BORDER"]
    TEXT, MUTED, BRAND = t["TEXT"], t["MUTED"], t["BRAND"]
    GOOD, BAD, WARN = t["GOOD"], t["BAD"], t["WARN"]
    APPROVE_FG, DENY_FG = t["APPROVE_FG"], t["DENY_FG"]
    STOP_BG, STOP_FG = t["STOP_BG"], t["STOP_FG"]
    BANNER_BG, BANNER_FG = t["BANNER_BG"], t["BANNER_FG"]
    URL_FG, COPY_FG = t["URL_FG"], t["COPY_FG"]
    CHECK_BG, CHECK_BORDER = t["CHECK_BG"], t["CHECK_BORDER"]
    CHECK_FG, CHECK_SEL = t["CHECK_FG"], t["CHECK_SEL"]
    NEUTRAL_BG, NEUTRAL_FG = t["NEUTRAL_BG"], t["NEUTRAL_FG"]


_THEME_CHOICE = _load_theme_choice()
_apply_theme(_resolve_theme(_THEME_CHOICE))


# --------------------------------------------------------------------------- #
# Small drawing/animation helpers. Tk has no per-widget alpha, so "fade" effects
# interpolate colors toward the window background instead.
# --------------------------------------------------------------------------- #

def _hex_to_rgb(c):
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _lerp_hex(c0, c1, t):
    """Blend from color c0 to c1 (t in 0..1), returning '#rrggbb'."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    (r0, g0, b0), (r1, g1, b1) = _hex_to_rgb(c0), _hex_to_rgb(c1)
    return "#%02x%02x%02x" % (
        round(r0 + (r1 - r0) * t),
        round(g0 + (g1 - g0) * t),
        round(b0 + (b1 - b0) * t),
    )


def _round_rect_points(x1, y1, x2, y2, r):
    """Point list for a smooth-polygon rounded rectangle."""
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


# --------------------------------------------------------------------------- #
# TLS: verify against the OS trust store (matches the browser / curl), so a
# stale bundled CA list in the frozen .exe does not cause "certificate expired".
# --------------------------------------------------------------------------- #

def build_ssl_context():
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #

mouse = MouseController()
keyboard = KeyboardController()

BUTTONS = {0: Button.left, 1: Button.middle, 2: Button.right}

SPECIAL_KEYS = {
    "Enter": Key.enter, "Backspace": Key.backspace, "Tab": Key.tab,
    "Escape": Key.esc, "ArrowUp": Key.up, "ArrowDown": Key.down,
    "ArrowLeft": Key.left, "ArrowRight": Key.right, "Shift": Key.shift,
    "Control": Key.ctrl, "Alt": Key.alt, "Meta": Key.cmd, "OS": Key.cmd,
    "CapsLock": Key.caps_lock, "Delete": Key.delete, "Home": Key.home,
    "End": Key.end, "PageUp": Key.page_up, "PageDown": Key.page_down,
    "Insert": Key.insert, " ": Key.space,
    "F1": Key.f1, "F2": Key.f2, "F3": Key.f3, "F4": Key.f4,
    "F5": Key.f5, "F6": Key.f6, "F7": Key.f7, "F8": Key.f8,
    "F9": Key.f9, "F10": Key.f10, "F11": Key.f11, "F12": Key.f12,
}

CODE_CHARS = {
    **{f"Key{c}": c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{f"Digit{d}": d for d in "0123456789"},
    "Minus": "-", "Equal": "=", "BracketLeft": "[", "BracketRight": "]",
    "Backslash": "\\", "Semicolon": ";", "Quote": "'", "Backquote": "`",
    "Comma": ",", "Period": ".", "Slash": "/", "Space": " ",
    **{f"Numpad{d}": d for d in "0123456789"},
    "NumpadAdd": "+", "NumpadSubtract": "-", "NumpadMultiply": "*",
    "NumpadDivide": "/", "NumpadDecimal": ".",
}


def resolve_key(ev):
    key = ev.get("key")
    code = ev.get("code")
    if key in SPECIAL_KEYS:
        return SPECIAL_KEYS[key]
    if code in CODE_CHARS:
        return KeyCode.from_char(CODE_CHARS[code])
    if key and len(key) == 1:
        return KeyCode.from_char(key.lower())
    return None


def _wheel_steps(delta):
    if not delta:
        return 0
    steps = int(round(delta / 100.0))
    return steps if steps else (1 if delta > 0 else -1)


def _in_rects(x, y, rects):
    """True if (x, y) is inside ANY of the rects (each left, top, right, bottom)."""
    if not rects:
        return False
    for left, top, right, bottom in rects:
        if left <= x <= right and top <= y <= bottom:
            return True
    return False


def apply_event(ev, monitors=None, window_rects=None, blocked=None,
                pressed=None, agent_foreground=False):
    """Apply one remote input event, while protecting the agent's OWN windows so a
    remote user can't close it or drive its controls:

    - Mouse CLICKS/SCROLL that land on any agent window (window_rects: the main
      window plus any open dialog, full frames incl. the title bar, in physical
      screen coords) are ignored. Cursor MOVEMENT may still pass over them.
    - Injected KEYBOARD is dropped while an agent window is foreground
      (agent_foreground), so injected Alt+F4 / Tab-to-a-button can't close or press
      the agent.
    The LOCAL user's real input is never affected (only injected input is filtered).

    `blocked` tracks buttons whose press was suppressed (so the matching release is
    suppressed too — no stuck button). `pressed` tracks buttons we actually injected
    a press for, so the caller can release them if the session ends mid-drag.
    """
    t = ev.get("t")
    # Mouse coords arrive in the TARGET monitor's pixel space (field "m"); add
    # that monitor's virtual-desktop origin to get absolute coords. Keyboard
    # events carry no "m" and are applied globally.
    off_x = off_y = 0
    if monitors:
        m = ev.get("m", 0)
        try:
            m = int(m)
        except (TypeError, ValueError):
            m = 0
        mon = monitors[m] if 0 <= m < len(monitors) else monitors[0]
        off_x = mon["left"]
        off_y = mon["top"]
    try:
        if t == "mm":
            mouse.position = (off_x + ev["x"], off_y + ev["y"])
        elif t == "md":
            ax, ay = off_x + ev["x"], off_y + ev["y"]
            b = ev.get("b", 0)
            if _in_rects(ax, ay, window_rects):
                if blocked is not None:
                    blocked.add(b)  # suppress this press (and its matching release)
                return
            if blocked is not None:
                blocked.discard(b)
            mouse.position = (ax, ay)
            mouse.press(BUTTONS.get(b, Button.left))
            if pressed is not None:
                pressed.add(b)
        elif t == "mu":
            b = ev.get("b", 0)
            if blocked is not None and b in blocked:
                blocked.discard(b)
                return  # its press was suppressed; don't inject a lone release
            mouse.position = (off_x + ev["x"], off_y + ev["y"])
            mouse.release(BUTTONS.get(b, Button.left))
            if pressed is not None:
                pressed.discard(b)
        elif t == "scroll":
            ax, ay = off_x + ev["x"], off_y + ev["y"]
            if _in_rects(ax, ay, window_rects):
                return  # don't scroll an agent window
            mouse.position = (ax, ay)
            sx = _wheel_steps(ev.get("dx", 0))
            sy = _wheel_steps(ev.get("dy", 0))
            if sx or sy:
                mouse.scroll(sx, -sy)
        elif t == "kd":
            if agent_foreground:
                return  # don't let injected keys drive the agent's own window
            k = resolve_key(ev)
            if k is not None:
                keyboard.press(k)
        elif t == "ku":
            if agent_foreground:
                return
            k = resolve_key(ev)
            if k is not None:
                keyboard.release(k)
    except Exception:
        pass  # never let a bad event kill the stream


# --------------------------------------------------------------------------- #
# Screen capture
# --------------------------------------------------------------------------- #

def _even(n):
    """Round down to an even number >= 2 (VP8 wants even frame dimensions)."""
    n = int(n) & ~1
    return n if n >= 2 else 2


class CaptureManager:
    """One background thread that owns a single ScreenGrabber and keeps the latest
    RGB frame for each monitor in a slot. mss is not safe to share across threads,
    so all grabbing happens here; ScreenTrack.recv() just reads the latest slot.

    Monitors the viewer currently wants are grabbed at the full frame rate; the
    rest are grabbed slowly (~2 fps) so their tracks stay alive without forcing an
    SDP renegotiation when the viewer switches monitors."""

    IDLE_INTERVAL = 0.5  # unwanted monitors: ~2 fps
    STALE_SECS = 5.0     # no successful grab for this long -> capture is dead

    def __init__(self, client):
        self.client = client
        self.monitors = client.monitors
        self.fps = max(1, int(client.args.fps))
        self._frames = {}            # monitor index -> latest RGB ndarray (h, w, 3)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_ok = None         # monotonic time of the last successful grab
        self._stall_signaled = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def latest(self, i):
        with self._lock:
            return self._frames.get(i)

    def _run(self):
        full_interval = 1.0 / self.fps
        last = {m["i"]: 0.0 for m in self.monitors}
        # Start the stall clock now: if capture never works, STALE_SECS later we
        # signal rather than sit on a black/frozen screen forever.
        self._last_ok = time.monotonic()
        with ScreenGrabber() as sct:
            while not self._stop.is_set():
                now = time.time()
                wanted = set(self.client.wanted or [m["i"] for m in self.monitors])
                next_due = now + full_interval
                for m in self.monitors:
                    i = m["i"]
                    interval = full_interval if i in wanted else self.IDLE_INTERVAL
                    if now - last[i] < interval:
                        next_due = min(next_due, last[i] + interval)
                        continue
                    last[i] = now
                    arr = self._grab(sct, m)
                    if arr is not None:
                        self._last_ok = time.monotonic()
                        with self._lock:
                            self._frames[i] = arr
                # Capture has stopped producing frames (e.g. secure desktop / lock
                # screen / display change): end the session so the viewer isn't left
                # on a frozen image while input still reaches the machine.
                if (not self._stall_signaled and
                        time.monotonic() - self._last_ok > self.STALE_SECS):
                    self._stall_signaled = True
                    self._signal_stall()
                sleep = max(0.005, min(full_interval, next_due - time.time()))
                self._stop.wait(sleep)

    def _signal_stall(self):
        loop = self.client.loop
        if loop and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self.client._on_capture_stalled())
                )
            except Exception:
                pass

    def _grab(self, sct, m):
        try:
            shot = sct.grab({"left": m["left"], "top": m["top"],
                             "width": m["width"], "height": m["height"]})
            scale = self.client.scale_for(m["width"])
            ow, oh = _even(shot.width * scale), _even(shot.height * scale)
            if (ow, oh) != (shot.width, shot.height):
                img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
                arr = np.asarray(img.resize((ow, oh)), dtype=np.uint8)
            else:
                arr = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(
                    (shot.height, shot.width, 3))
            return np.ascontiguousarray(arr)
        except Exception:
            return None


class ScreenTrack(MediaStreamTrack):
    """A WebRTC video track feeding one monitor's frames (from CaptureManager) to
    the encoder. Paced at the configured --fps; carries its monitor index (`mi`)
    so the offer can tell the viewer which track is which monitor."""

    kind = "video"
    _CLOCK = 90000

    def __init__(self, client, i):
        super().__init__()
        self.client = client
        self.mi = i
        self._interval = 1.0 / max(1, int(client.args.fps))
        self._pts = 0
        self._next = None

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        now = time.time()
        if self._next is None:
            self._next = now
        delay = self._next - now
        if delay > 0:
            await asyncio.sleep(delay)
        self._next += self._interval
        arr = await self._current()
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        self._pts += int(self._interval * self._CLOCK)
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, self._CLOCK)
        return frame

    async def _current(self):
        cap = self.client.capture
        arr = cap.latest(self.mi) if cap else None
        if arr is None:
            for _ in range(200):  # wait up to ~2s for the first grab
                await asyncio.sleep(0.01)
                cap = self.client.capture
                arr = cap.latest(self.mi) if cap else None
                if arr is not None:
                    break
        if arr is None:
            arr = np.zeros((2, 2, 3), dtype=np.uint8)  # keep the track alive
        return arr


def _compute_blockrects(client):
    """The agent's own windows, each expressed per-monitor in that monitor's local
    pixel space (the same space the viewer's canvas uses), so the viewer can show a
    'blocked' cursor over them. _window_rects are absolute physical screen coords."""
    wins = getattr(client.app, "_window_rects", None) or ()
    out = []
    for mon in client.monitors:
        ml, mt = mon["left"], mon["top"]
        mr, mb = ml + mon["width"], mt + mon["height"]
        for (l, t, r, b) in wins:
            il, it, ir, ib = max(l, ml), max(t, mt), min(r, mr), min(b, mb)
            if ir > il and ib > it:
                out.append({"m": mon["i"], "x": il - ml, "y": it - mt,
                            "w": ir - il, "h": ib - it})
    return out


async def _maybe_send_blockrects(ws, client):
    """Send the agent's window rects to the viewer, but only when they change."""
    rects = _compute_blockrects(client)
    key = json.dumps(rects, sort_keys=True)
    if key != client._last_blockrects:
        client._last_blockrects = key
        await ws.send(json.dumps({"type": "blockrects", "rects": rects}))


async def blockrects_loop(ws, client):
    """Keep the viewer's 'blocked cursor' regions (where the agent's own windows
    are) up to date over the WebSocket, for as long as a session is live. Sends
    only when the rects change (see _maybe_send_blockrects)."""
    try:
        while client.streaming:
            try:
                await _maybe_send_blockrects(ws, client)
            except asyncio.CancelledError:
                break
            except Exception:
                break
            await asyncio.sleep(0.4)
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# Networking helpers
# --------------------------------------------------------------------------- #

def normalize_server(server):
    server = server.strip().rstrip("/")
    if server.startswith("http://"):
        return server, "ws://" + server[len("http://"):]
    if server.startswith("https://"):
        return server, "wss://" + server[len("https://"):]
    return "https://" + server, "wss://" + server


def request_session(http_base, ctx):
    req = urllib.request.Request(
        f"{http_base}/api/new-session",
        data=b"",
        method="POST",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))["code"]


STUN_FALLBACK = [{"urls": ["stun:stun.cloudflare.com:3478"]}]


def request_ice_servers(http_base, ctx, code):
    """Fetch ICE servers (STUN + minted TURN when configured) from the Worker.
    Falls back to STUN-only so a peer connection can still be attempted."""
    try:
        req = urllib.request.Request(
            f"{http_base}/api/ice-servers?code={code}",
            method="GET",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        servers = data.get("iceServers")
        if isinstance(servers, list) and servers:
            return servers
    except Exception:
        pass
    return STUN_FALLBACK


# --------------------------------------------------------------------------- #
# Network client (runs its own asyncio loop on a background thread)
# --------------------------------------------------------------------------- #

class NetClient:
    def __init__(self, app, args):
        self.app = app
        self.args = args
        self.ssl_ctx = build_ssl_context()
        self.loop = None
        self.stop_event = None
        self.ws = None
        self.streaming = False
        self.pc = None              # RTCPeerConnection carrying the video tracks
        self.capture = None         # CaptureManager (screen grab -> frame slots)
        self.blockrects_task = None # keeps the viewer's blocked-cursor regions fresh
        self.monitors = []          # list of {"i","left","top","width","height"}
        self.primary = None         # convenience handle to monitors[0]
        self.wanted = []            # 0-based indices the viewer wants streamed
        self.code = None
        self._http_base = None      # https base, for the ICE-servers request
        self._left_reported = False # viewer-gone reported once per session (dedup)
        self._blocked_btns = set()  # buttons whose press was suppressed over our window
        self._pressed_btns = set()  # buttons we actually injected a press for (to release)
        self._last_blockrects = None  # last blockrects JSON sent to the viewer (dedup)
        # True between "we ended the session locally" and the resulting peer-left,
        # so the status reads "you closed the connection" rather than blaming the
        # viewer for a disconnect we initiated.
        self.ended_locally = False

    def start(self):
        threading.Thread(target=self._thread_main, daemon=True).start()

    def request_stop(self):
        if self.loop and self.stop_event and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.stop_event.set)
            except Exception:
                pass

    def end_session(self):
        """End the current viewer session but keep the agent connected and the
        program running, ready for a new connection."""
        if self.loop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._end_session())
                )
            except Exception:
                pass

    async def _end_session(self):
        # Mark this as a local stop so the peer-left that follows (the viewer
        # closing in response to "session-ended") is reported as us closing the
        # connection, not the viewer leaving.
        self.ended_locally = True
        await self._stop_stream()
        if self.ws:
            try:
                # Tell the viewer promptly so it drops out of the session; it then
                # closes, which frees the single-session slot for a new viewer.
                await self.ws.send(json.dumps({"type": "session-ended"}))
            except Exception:
                pass

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as e:
            self.app.post(lambda: self.app.set_status(f"Error: {e}", BAD))

    async def _run(self):
        self.loop = asyncio.get_event_loop()
        self.stop_event = asyncio.Event()

        # NOTE on coordinates: instantiating mss (ScreenGrabber) makes the PROCESS
        # per-monitor DPI-aware as a side effect, so from here on screen capture,
        # the reported monitor origins, pynput cursor positioning, and Win32
        # GetWindowRect (used for the window-click filter) are ALL in the same
        # PHYSICAL pixel space — which is why the filter's rects line up with the
        # injected click coordinates. We don't set awareness ourselves; if that mss
        # behavior ever changed, these would need to be reconciled explicitly.
        with ScreenGrabber() as sct:
            raw_mons = [dict(m) for m in sct.monitors]
        # sct.monitors[0] is the virtual "all monitors" rectangle; the physical
        # monitors are [1:]. Index them 0-based (the index that travels in frames
        # and input). Fall back to the virtual desktop if nothing physical shows.
        phys = raw_mons[1:] if len(raw_mons) > 1 else raw_mons[:1]
        self.monitors = [
            {"i": idx, "left": m["left"], "top": m["top"],
             "width": m["width"], "height": m["height"]}
            for idx, m in enumerate(phys)
        ]
        self.primary = self.monitors[0] if self.monitors else None
        # Default: stream every monitor until the viewer sends a "want" message.
        self.wanted = [m["i"] for m in self.monitors]

        http_base, ws_base = normalize_server(self.args.server)
        self._http_base = http_base
        self.app.post(lambda: self.app.set_status(tr("agent.status.requesting"), MUTED))
        try:
            code = await self.loop.run_in_executor(
                None, lambda: request_session(http_base, self.ssl_ctx)
            )
        except Exception as e:
            self.app.post(
                lambda: self.app.on_fatal(tr("agent.error.unreachable") + "\n" + str(e))
            )
            return
        self.code = code
        self.app.post(lambda: self.app.on_code(code, http_base))

        ws_url = f"{ws_base}/ws/agent?code={code}"
        # Only use TLS for wss:// (a plaintext ws:// server — e.g. a local dev
        # worker — rejects an ssl context).
        ws_ssl = self.ssl_ctx if ws_url.startswith("wss://") else None
        try:
            async with websockets.connect(
                ws_url,
                ssl=ws_ssl,
                max_size=None,
                ping_interval=20,
                user_agent_header=USER_AGENT,
            ) as ws:
                self.ws = ws
                self.app.post(
                    lambda: self.app.set_status(tr("agent.status.waiting"), WARN)
                )
                recv = asyncio.create_task(self._recv_loop(ws))
                stop = asyncio.create_task(self.stop_event.wait())
                _, pending = await asyncio.wait(
                    {recv, stop}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await self._stop_stream()
        except Exception as e:
            self.app.post(lambda: self.app.set_status(f"Disconnected: {e}", BAD))

    async def _recv_loop(self, ws):
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if "t" in msg:
                # Only inject input for an APPROVED, streaming session (ignore
                # anything sent before consent / during the prompt / after a deny),
                # and protect the agent's own windows (see apply_event).
                if not self.streaming:
                    continue
                apply_event(
                    msg, self.monitors,
                    getattr(self.app, "_window_rects", None),
                    self._blocked_btns, self._pressed_btns,
                    getattr(self.app, "_agent_foreground", False),
                )
                continue
            mtype = msg.get("type")
            if mtype == "want":
                self._set_wanted(msg.get("monitors"))
            elif mtype == "webrtc-answer":
                if self.pc:
                    try:
                        await self.pc.setRemoteDescription(
                            RTCSessionDescription(sdp=msg.get("sdp", ""), type="answer")
                        )
                    except Exception:
                        pass
            elif mtype == "webrtc-ice":
                await self._add_remote_candidate(msg.get("candidate"))
            elif mtype == "viewer-joined":
                asyncio.create_task(self._on_viewer(ws))
            elif mtype == "peer-left" and msg.get("who") == "viewer":
                await self._stop_stream()
                local = self.ended_locally
                self.ended_locally = False
                self._report_peer_left(local)

    def _report_peer_left(self, local):
        """Report the viewer gone to the UI exactly once per session. Two paths can
        detect it (the relayed WS peer-left and the WebRTC connectionstatechange),
        and they can race; this dedups so the banner/stop teardown runs once."""
        if self._left_reported:
            return
        self._left_reported = True
        self.app.post(lambda: self.app.on_peer_left(local))

    async def _on_capture_stalled(self):
        """Screen capture stopped producing frames (lock screen, UAC/secure desktop,
        a display change, ...). End the session so the viewer drops out instead of
        sitting on a frozen frame while input still reaches this machine."""
        if not self.streaming:
            return
        ws = self.ws
        await self._stop_stream()
        if ws:
            try:
                await ws.send(json.dumps({"type": "session-ended"}))
            except Exception:
                pass
        self._report_peer_left(False)

    async def _on_viewer(self, ws):
        if self.streaming:
            return
        # A fresh viewer: clear any leftover per-session input state.
        self.ended_locally = False
        self._left_reported = False
        self._blocked_btns.clear()
        self._pressed_btns.clear()
        self._last_blockrects = None  # force a fresh blockrects send to this viewer
        if self.app.auto_accept:
            allowed = True
        else:
            allowed = await self.loop.run_in_executor(None, self._ask_approval)
        if not allowed:
            try:
                await ws.send(json.dumps({"type": "denied"}))
            except Exception:
                pass
            self.app.post(self.app.on_denied)
            return
        # Tell the viewer the monitor list so it can build its <video> elements.
        try:
            await ws.send(json.dumps({
                "type": "monitors",
                "monitors": [
                    {"i": m["i"], "w": m["width"], "h": m["height"]}
                    for m in self.monitors
                ],
            }))
        except Exception:
            return
        self.streaming = True
        # Start grabbing the screen and open the WebRTC video connection.
        self.capture = CaptureManager(self)
        self.capture.start()
        ok = await self._start_webrtc(ws)
        if not ok:
            await self._stop_stream()
            self._report_peer_left(False)
            return
        # Blockrects used to piggyback on the capture loop; now its own task.
        self.blockrects_task = asyncio.create_task(blockrects_loop(ws, self))
        self.app.post(self.app.on_connected)

    async def _start_webrtc(self, ws):
        """Build the peer connection: one send-only video track per monitor, create
        the offer (aiortc gathers ICE non-trickle, so candidates ride in the SDP),
        and send it with a mid->monitor map. Returns False on failure."""
        try:
            ice = await self.loop.run_in_executor(
                None,
                lambda: request_ice_servers(self._http_base, self.ssl_ctx, self.code),
            )
            servers = [
                RTCIceServer(
                    urls=s.get("urls"),
                    username=s.get("username"),
                    credential=s.get("credential"),
                )
                for s in ice
                if s.get("urls")
            ]
            self.pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))

            @self.pc.on("connectionstatechange")
            async def _on_conn_state():
                pc = self.pc
                if not pc or not self.streaming:
                    return
                if pc.connectionState in ("failed", "closed"):
                    # The media path died; fall back to waiting for a new viewer.
                    await self._stop_stream()
                    self._report_peer_left(False)

            for m in self.monitors:
                self.pc.addTrack(ScreenTrack(self, m["i"]))

            await self.pc.setLocalDescription(await self.pc.createOffer())
            self._apply_bitrate()

            mids = {}
            for tcv in self.pc.getTransceivers():
                track = tcv.sender.track if tcv.sender else None
                mi = getattr(track, "mi", None)
                if mi is not None and tcv.mid is not None:
                    mids[tcv.mid] = mi

            await ws.send(json.dumps({
                "type": "webrtc-offer",
                "sdp": self.pc.localDescription.sdp,
                "mids": mids,
            }))
            return True
        except Exception:
            return False

    def _apply_bitrate(self):
        """Best-effort per-sender max bitrate from --bitrate (kbps). aiortc may not
        honor it on every version; failures are harmless (congestion control still
        adapts)."""
        if not self.args.bitrate or not self.pc:
            return
        for sender in self.pc.getSenders():
            try:
                params = sender.getParameters()
                if params and params.encodings:
                    for enc in params.encodings:
                        enc.maxBitrate = int(self.args.bitrate) * 1000
                    asyncio.ensure_future(sender.setParameters(params))
            except Exception:
                pass

    async def _add_remote_candidate(self, c):
        """Add a trickled ICE candidate from the viewer (the browser trickles its
        own; the agent gathered non-trickle, so this is the only trickle path)."""
        if not self.pc or not c:
            return
        cand = c.get("candidate")
        if not cand:
            return  # end-of-candidates marker
        try:
            sdp = cand.split(":", 1)[1] if cand.startswith("candidate:") else cand
            ice = candidate_from_sdp(sdp)
            ice.sdpMid = c.get("sdpMid")
            ice.sdpMLineIndex = c.get("sdpMLineIndex")
            await self.pc.addIceCandidate(ice)
        except Exception:
            pass

    def _ask_approval(self):
        ev = threading.Event()
        box = {"ok": False}
        self.app.post(lambda: self.app.show_approval(ev, box))
        ev.wait()
        return box["ok"]

    def _set_wanted(self, monitors):
        """Set the monitors the agent currently streams. Keep only valid, in-range
        indices; if the result is empty, fall back to streaming all monitors."""
        valid = {m["i"] for m in self.monitors}
        req = []
        for i in (monitors or []):
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if i in valid and i not in req:
                req.append(i)
        self.wanted = req if req else [m["i"] for m in self.monitors]

    def scale_for(self, width):
        """Per-monitor downscale factor based on --scale / --max-width."""
        if self.args.scale:
            return self.args.scale
        if width > 0:
            return min(1.0, self.args.max_width / width)
        return 1.0

    async def _stop_stream(self):
        self.streaming = False
        self._release_held_buttons()
        if self.blockrects_task:
            self.blockrects_task.cancel()
            try:
                await self.blockrects_task
            except (asyncio.CancelledError, Exception):
                # Awaiting a cancelled task re-raises CancelledError, which is a
                # BaseException and would otherwise escape "except Exception",
                # skipping on_peer_left and leaving the UI stuck on "Connected".
                pass
            self.blockrects_task = None
        if self.pc:
            pc = self.pc
            self.pc = None
            try:
                await pc.close()
            except Exception:
                pass
        if self.capture:
            cap = self.capture
            self.capture = None
            try:
                await self.loop.run_in_executor(None, cap.stop)
            except Exception:
                pass

    def _release_held_buttons(self):
        """Release any mouse button the remote pressed but didn't release (e.g. the
        viewer disconnected mid-drag), so it can't stay stuck down on this machine."""
        for b in list(self._pressed_btns):
            try:
                mouse.release(BUTTONS.get(b, Button.left))
            except Exception:
                pass
        self._pressed_btns.clear()
        self._blocked_btns.clear()


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

class RoundedButton(tk.Canvas):
    """A rounded-corner button drawn on a Canvas that AUTO-SIZES to its own
    label.

    The canvas is sized from live tkinter.font measurements (text pixels) plus
    padding rather than from hardcoded pixels. Canvas item coordinates and
    ``font.measure()`` are both expressed in real pixels, so they track each
    other at any Tk scaling / Windows DPI: the label can never overrun the
    canvas and stays centered. Height is derived from the font metrics alone, so
    every button sharing a font/padding is exactly the same height regardless of
    its label; ``set_width`` lets callers give two buttons a matching width.
    """

    def __init__(self, parent, text, bg, fg, command=None,
                 radius=14, pad_x=22, pad_y=12, min_width=120,
                 font=None, parent_bg=CARD, hover_outline=None, border=None):
        super().__init__(parent, bg=parent_bg, highlightthickness=0, bd=0,
                         takefocus=0)
        self.command = command
        # Hover/press feedback mirroring the web buttons: a brand-colored border
        # fades in on hover, the fill lifts slightly, and it darkens while pressed.
        self._hover_outline = BRAND if hover_outline is None else hover_outline
        # Optional resting border (like the web .btn's 1px --border), so low-contrast
        # fills still read as a button against the surface behind them.
        self._border = border
        self._hover_t = 0.0
        self._hover_target = 0.0
        self._hover_anim = None
        self._pressed = False
        # NOTE: never assign to self._w / self._h — tkinter reserves self._w for
        # the widget's Tcl command name. Custom fields use different names.
        self._radius = radius
        self._pad_x = pad_x
        self._pad_y = pad_y
        self._min_width = min_width
        self._bg, self._fg, self._text = bg, fg, text
        # Home (fully-shown) colors, kept so set_fade() can animate toward them.
        self._home_bg, self._home_fg, self._home_parent_bg = bg, fg, parent_bg
        # A concrete Font object so measure()/metrics() match what is drawn.
        if font is None:
            self._font = tkfont.Font(family="Segoe UI Semibold", size=13)
        elif isinstance(font, tkfont.Font):
            self._font = font
        else:
            self._font = tkfont.Font(font=font)
        self._req_w = 0
        self._bw = 0
        self._bh = 0
        self._measure()
        self._render()
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Destroy>", self._on_destroy)
        self.configure(cursor="hand2")

    def _on_destroy(self, _e):
        # Cancel a pending hover animation so its after-callback can't fire against
        # the destroyed widget ("invalid command name").
        if self._hover_anim is not None:
            try:
                self.after_cancel(self._hover_anim)
            except Exception:
                pass
            self._hover_anim = None

    def _measure(self):
        text_w = self._font.measure(self._text)
        text_h = self._font.metrics("linespace")
        self._req_w = text_w + 2 * self._pad_x
        self._bh = text_h + 2 * self._pad_y
        self._bw = max(self._req_w, self._min_width)
        self.configure(width=self._bw, height=self._bh)

    @property
    def req_width(self):
        """Natural width (measured text + horizontal padding), in pixels."""
        return self._req_w

    def set_width(self, width):
        """Widen the button to a shared width (never below its own text need)."""
        self._bw = max(int(width), self._req_w)
        self.configure(width=self._bw)
        self._render()

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kw)

    def _display_fill(self):
        f = self._bg
        if self._pressed:
            return _lerp_hex(f, "#000000", 0.14)   # darken while pressed
        if self._hover_t > 0:
            return _lerp_hex(f, "#ffffff", 0.12 * self._hover_t)  # lift on hover
        return f

    def _render(self):
        self.delete("all")
        r = max(0, min(self._radius, self._bh // 2 - 1, self._bw // 2 - 1))
        fill = self._display_fill()
        # Brand-colored border fades in on hover; at rest use the optional resting
        # border (or the fill, i.e. invisible, when none was set). Width 2.
        if self._hover_t > 0:
            outline = _lerp_hex(fill, self._hover_outline, self._hover_t)
        elif self._border is not None:
            outline = self._border
        else:
            outline = fill
        self._round_rect(1, 1, self._bw - 1, self._bh - 1, r,
                         fill=fill, outline=outline, width=2)
        self.create_text(self._bw / 2, self._bh / 2, text=self._text,
                         fill=self._fg, font=self._font)

    def _on_press(self, _e):
        self._pressed = True
        self._render()

    def _on_release(self, e):
        was = self._pressed
        self._pressed = False
        self._render()
        inside = 0 <= e.x <= self._bw and 0 <= e.y <= self._bh
        if was and inside and self.command:
            self.command()

    def _set_hover(self, on):
        self._hover_target = 1.0 if on else 0.0
        if self._hover_anim is None:
            self._hover_step()

    def _hover_step(self):
        self._hover_anim = None
        cur, tgt = self._hover_t, self._hover_target
        d = tgt - cur
        if abs(d) < 0.08:
            self._hover_t = tgt
            self._render()
            return
        self._hover_t = cur + d * 0.4
        self._render()
        try:
            self._hover_anim = self.after(16, self._hover_step)
        except Exception:
            self._hover_t = tgt
            self._render()

    def set_command(self, command):
        self.command = command

    def set_text(self, text):
        self._text = text
        self._measure()
        self._render()

    def set_fade(self, t, bg0=None):
        """Fade the button from a start color (default: its own parent bg) toward
        its home colors: t=0 -> invisible (blends with bg0), t=1 -> full color."""
        bg0 = self._home_parent_bg if bg0 is None else bg0
        self._bg = _lerp_hex(bg0, self._home_bg, t)
        self._fg = _lerp_hex(bg0, self._home_fg, t)
        try:
            self.configure(bg=_lerp_hex(bg0, self._home_parent_bg, t))
        except Exception:
            pass
        self._render()


class RoundedPanel(tk.Canvas):
    """A rounded-corner box (matching the web app's boxes) holding centered,
    possibly multi-line text. It auto-sizes to its content; set_size() lets
    callers give several panels an identical size."""

    def __init__(self, parent, text, fill, fg, font, parent_bg=BG,
                 outline=BORDER, radius=14, pad_x=22, pad_y=14,
                 min_width=0, justify="center"):
        super().__init__(parent, bg=parent_bg, highlightthickness=0, bd=0,
                         takefocus=0)
        self._fill = fill
        self._outline = outline
        self._fg = fg
        self._text = text
        self._radius = radius
        self._pad_x = pad_x
        self._pad_y = pad_y
        self._min_width = min_width
        self._justify = justify
        self._font = font if isinstance(font, tkfont.Font) else tkfont.Font(font=font)
        self._bw = 0
        self._bh = 0
        self._req_w = 0
        self._req_h = 0
        self._measure()
        self._render()

    def _measure(self):
        lines = self._text.split("\n") or [""]
        text_w = max((self._font.measure(ln) for ln in lines), default=0)
        text_h = self._font.metrics("linespace") * max(1, len(lines))
        self._req_w = text_w + 2 * self._pad_x
        self._req_h = text_h + 2 * self._pad_y
        # grow-only: never shrink below a size set earlier via set_size()
        self._bw = max(self._bw, self._req_w, self._min_width)
        self._bh = max(self._bh, self._req_h)
        self.configure(width=self._bw, height=self._bh)

    @property
    def req_width(self):
        return self._req_w

    @property
    def req_height(self):
        return self._req_h

    def set_size(self, width, height):
        self._bw = max(int(width), self._req_w)
        self._bh = max(int(height), self._req_h)
        self.configure(width=self._bw, height=self._bh)
        self._render()

    def get_text(self):
        return self._text

    def set_text(self, text):
        self._text = text
        self._measure()
        self._render()

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kw)

    def _render(self):
        self.delete("all")
        r = max(0, min(self._radius, self._bh // 2 - 1, self._bw // 2 - 1))
        self._round_rect(1, 1, self._bw - 1, self._bh - 1, r,
                         fill=self._fill, outline=self._outline)
        self.create_text(self._bw / 2, self._bh / 2, text=self._text,
                         fill=self._fg, font=self._font, justify=self._justify)


class ActionDialog(tk.Toplevel):
    """A themed confirm/consent prompt shown as ITS OWN normal window: a message
    plus a negative (left) and a positive (right) button. It opens and closes like
    a standard Windows dialog (no fade animation), and on Windows 11 it gets native
    rounded corners. Closing it with the window's X or Escape runs the negative
    action."""

    def __init__(self, parent, message, pos_text, pos_bg, pos_fg,
                 neg_text, neg_bg, neg_fg, on_pos=None, on_neg=None,
                 title_text=None):
        super().__init__(parent)
        self._on_pos = on_pos
        self._on_neg = on_neg
        self._done = False
        self._topmost_after = None
        self.bind("<Destroy>", lambda e: self._on_destroy())
        self.title(title_text or tr("agent.window.title"))
        self.configure(bg=CARD)
        self.resizable(False, False)
        try:
            self.transient(parent)
        except Exception:
            pass
        try:  # match the main window's icon
            if getattr(parent, "_icon_imgs", None):
                self.iconphoto(False, *parent._icon_imgs)
        except Exception:
            pass

        wrap = tk.Frame(self, bg=CARD)
        wrap.pack(padx=26, pady=22)
        tk.Label(wrap, text=message, bg=CARD, fg=TEXT,
                 font=("Segoe UI", 11, "bold"), wraplength=330,
                 justify="center").pack(pady=(0, 18))
        row = tk.Frame(wrap, bg=CARD)
        row.pack()
        bfont = tkfont.Font(family="Segoe UI Semibold", size=12)
        self.neg_btn = RoundedButton(row, neg_text, neg_bg, neg_fg, font=bfont,
                                     parent_bg=CARD, command=self._choose_neg,
                                     border=BORDER)
        self.pos_btn = RoundedButton(row, pos_text, pos_bg, pos_fg, font=bfont,
                                     parent_bg=CARD, command=self._choose_pos,
                                     border=BORDER)
        w = max(self.neg_btn.req_width, self.pos_btn.req_width, 104)
        self.neg_btn.set_width(w)
        self.pos_btn.set_width(w)
        self.neg_btn.pack(side="left", padx=8)   # negative on the left
        self.pos_btn.pack(side="left", padx=8)   # positive on the right

        self.protocol("WM_DELETE_WINDOW", self._choose_neg)
        self.bind("<Escape>", lambda e: self._choose_neg())
        self.update_idletasks()
        self._center(parent)
        try:
            self.lift()
            self._safe_attr("-topmost", True)
            self._topmost_after = self.after(
                500, lambda: self._safe_attr("-topmost", False))
            self.grab_set()
            self.bell()
        except Exception:
            pass

    def _safe_attr(self, name, val):
        try:
            self.attributes(name, val)
        except Exception:
            pass

    def _center(self, parent):
        try:
            self.update_idletasks()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            self.geometry("+%d+%d" % (max(0, px + (pw - w) // 2),
                                      max(0, py + (ph - h) // 2)))
        except Exception:
            pass

    def _on_destroy(self):
        # Cancel the pending topmost-reset so it can't fire against the destroyed
        # window ("invalid command name").
        if self._topmost_after is not None:
            try:
                self.after_cancel(self._topmost_after)
            except Exception:
                pass
            self._topmost_after = None

    def _choose_pos(self):
        self._finish(self._on_pos)

    def _choose_neg(self):
        self._finish(self._on_neg)

    def _finish(self, cb):
        if self._done:
            return
        self._done = True
        try:
            self.grab_release()
        except Exception:
            pass
        if cb:
            try:
                cb()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass


class RoundedBanner(tk.Canvas):
    """Full-width top strip with square top corners and rounded BOTTOM corners,
    centered text, and a color-based fade in/out via set_fade_colors()."""

    def __init__(self, parent, parent_bg, fill, fg, font, radius=16):
        super().__init__(parent, bg=parent_bg, highlightthickness=0, bd=0,
                         takefocus=0)
        self._pbg = parent_bg
        self._radius = radius
        self._font = font if isinstance(font, tkfont.Font) else tkfont.Font(font=font)
        self._text = ""
        self._cur_fill = fill
        self._cur_fg = fg
        self.bind("<Configure>", lambda e: self._redraw())

    def set_text(self, text):
        self._text = text
        self._redraw()

    def set_fade_colors(self, fill, fg):
        self._cur_fill = fill
        self._cur_fg = fg
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 2 or h <= 2:
            return
        r = max(0, min(self._radius, h - 1, w // 2 - 1))
        # Top corners sharp (points repeated), bottom corners rounded (smoothed).
        pts = [
            0, 0, 0, 0, 0, 0,
            w, 0, w, 0, w, 0,
            w, h - r,
            w, h, w - r, h,
            r, h, 0, h,
            0, h - r,
        ]
        self.create_polygon(pts, smooth=True, fill=self._cur_fill,
                            outline=self._cur_fill)
        self.create_text(w / 2, h / 2, text=self._text, fill=self._cur_fg,
                         font=self._font)


class App:
    def __init__(self, args):
        self.args = args
        self.auto_accept = bool(args.auto_accept)
        self.q = queue.Queue()
        self.client = None
        self._anims = {}  # key -> pending after-id, for cancellable fade animations
        self._theme_choice = _THEME_CHOICE
        self._code = None            # last session code (kept across theme rebuilds)
        self._http_base = None
        self._status_text = ""
        self._status_role = "muted"
        self._connected = False      # a viewer is actively connected (streaming)
        self._approval_dialog = None
        self._window_rects = ()      # our windows' screen rects (main + any dialog);
        #                              injected clicks on them are ignored.
        self._agent_foreground = False  # an agent window has OS focus -> drop injected keys

        self.root = tk.Tk()
        self.root.title(tr("agent.window.title"))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # The window/taskbar icon and the header logo image are theme-independent,
        # so they are created once here; the rest of the UI is (re)built by
        # _build_ui(), which also runs on a live theme switch.
        self._icon_imgs = []
        for _b in (LOGO_ICONS_B64 or []):
            try:
                self._icon_imgs.append(tk.PhotoImage(data=_b))
            except Exception:
                pass
        if self._icon_imgs:
            try:
                self.root.iconphoto(True, *self._icon_imgs)
            except Exception:
                pass
        self._logo_small = None
        if LOGO_HEADER_B64:
            try:
                self._logo_small = tk.PhotoImage(data=LOGO_HEADER_B64)
            except Exception:
                self._logo_small = None

        self._drain_after = None
        self._build_ui()
        # Refresh our window rects / focus state immediately on move/resize/focus
        # change (the 80ms drain is only the backstop), so the click/key filter
        # can't be beaten by a fast window move.
        for _seq in ("<Configure>", "<FocusIn>", "<FocusOut>"):
            self.root.bind(_seq, lambda e: self._update_window_rects(), add="+")
        self._update_window_rects()
        self._drain_after = self.root.after(80, self._drain)

    def _build_ui(self):
        """Build (or, on a live theme switch, rebuild) the themed widgets. It reads
        the current theme color globals, so it reflects the active theme each time."""
        self.root.configure(bg=BG)
        self.root.resizable(True, True)  # relaxed for measurement; locked at the end

        if self._logo_small is not None:
            self.title_label = tk.Label(
                self.root, image=self._logo_small, text="  Audiobus Remote",
                compound="left", bg=BG, fg=BRAND, font=("Segoe UI Semibold", 16),
            )
        else:
            self.title_label = tk.Label(
                self.root, text="◇ Audiobus Remote", bg=BG, fg=BRAND,
                font=("Segoe UI Semibold", 16),
            )
        self.title_label.pack(pady=(18, 2))
        self.subtitle = tk.Label(
            self.root, text=tr("agent.subtitle"), bg=BG, fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.subtitle.pack()

        # Reserve two lines so a long status (e.g. "Viewer disconnected. Waiting
        # for someone to connect…") wraps to fit the fixed-width window without
        # shifting the layout; short messages just use the first line.
        self.status = tk.Label(
            self.root, text=tr("agent.status.starting"), bg=BG, fg=MUTED, font=("Segoe UI", 10),
            wraplength=250, justify="center", height=2,
        )
        self.status.pack(pady=(14, 6))

        tk.Label(
            self.root, text=tr("agent.sessionCode"), bg=BG, fg=MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack()
        # Session-code box: rounded, near-black, big monospace code. padx=32
        # reproduces the previous version's box width, which is the target size
        # for both boxes.
        # Same fill as the "Connect at:" box (CARD) so both read as boxes against
        # the window background in light and dark themes.
        self.code_panel = RoundedPanel(
            self.root, "··········", fill=CARD, fg=TEXT,
            font=("Consolas", 26, "bold"), parent_bg=BG, outline=BORDER,
            pad_x=32, pad_y=14,
        )
        self.code_panel.pack(pady=(4, 6))

        # Copy button: rounded, brand-blue so it stands out (always active — the
        # copy_code handler ignores clicks while the code is still a placeholder).
        self.copy_btn = RoundedButton(
            self.root, tr("agent.btn.copy"), BRAND, COPY_FG, command=self.copy_code,
            font=("Segoe UI Semibold", 10), min_width=130, parent_bg=BG,
        )
        self.copy_btn.pack(pady=(2, 2))

        # Both boxes take the session-code box's (previous-version) size.
        shared_w = self.code_panel.req_width
        shared_h = self.code_panel.req_height

        # "Connect at:" box: same size, rounded, a different (elevated) colour.
        # The URL has no spaces to wrap at, so pick the largest font size that
        # still fits it on one line inside this width.
        _http_base, _ = normalize_server(self.args.server)
        self._http_base = _http_base
        url_line = f"{_http_base}/connect"
        url_pad_x = 10
        avail = shared_w - 2 * url_pad_x
        url_size = 11
        _uf = tkfont.Font(family="Segoe UI", size=url_size, weight="bold")
        while url_size > 6 and _uf.measure(url_line) > avail:
            url_size -= 1
            _uf = tkfont.Font(family="Segoe UI", size=url_size, weight="bold")
        self.url_panel = RoundedPanel(
            self.root, tr("agent.connectAt") + "\n" + url_line,
            fill=CARD, fg=URL_FG, font=_uf, parent_bg=BG,
            outline=BORDER, pad_x=url_pad_x, pad_y=12,
        )
        self.url_panel.pack(pady=(8, 4))

        self.code_panel.set_size(shared_w, shared_h)
        self.url_panel.set_size(shared_w, shared_h)

        self.auto_var = tk.BooleanVar(value=self.auto_accept)
        # The checkbox sits on a rounded ORANGE panel with a bold label and a bold,
        # high-contrast border so it stands clearly apart from the other boxes.
        self.auto_wrap = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0,
                                   takefocus=0)
        self.auto_chk = tk.Checkbutton(
            self.auto_wrap, text=tr("agent.autoAccept"),
            variable=self.auto_var, command=self._toggle_auto,
            bg=CHECK_BG, fg=CHECK_FG, selectcolor=CHECK_SEL, activebackground=CHECK_BG,
            activeforeground=CHECK_FG, font=("Segoe UI", 9, "bold"), borderwidth=0,
            highlightthickness=0,
        )
        self.auto_chk.update_idletasks()
        _apx, _apy = 16, 8
        _abw = self.auto_chk.winfo_reqwidth() + 2 * _apx
        _abh = self.auto_chk.winfo_reqheight() + 2 * _apy
        self.auto_wrap.configure(width=_abw, height=_abh)
        self.auto_wrap.create_polygon(
            _round_rect_points(2, 2, _abw - 2, _abh - 2, 12),
            smooth=True, fill=CHECK_BG, outline=CHECK_BORDER, width=2,
        )
        self.auto_wrap.create_window(_abw / 2, _abh / 2, window=self.auto_chk)
        self.auto_wrap.pack(pady=(2, 6))

        # Theme selector (Auto / Light / Dark), applied live.
        self._build_theme_selector()

        # The approval prompt is shown as its own window (ActionDialog), created on
        # demand in show_approval(); it is no longer an in-window overlay.

        # Connected banner (hidden until streaming) — rounded bottom, fades in/out.
        self.banner = RoundedBanner(
            self.root, parent_bg=BG, fill=BANNER_BG, fg=BANNER_FG,
            font=tkfont.Font(family="Segoe UI Semibold", size=10),
        )

        self.stop_btn = RoundedButton(
            self.root, tr("agent.btn.stop"), STOP_BG, STOP_FG,
            command=self.end_session_click,
            font=("Segoe UI Semibold", 10), min_width=150, parent_bg=BG,
        )
        # Packed now so the base-layout measurement below RESERVES room for it in
        # the fixed window height; it is pack_forget()'d again before the window
        # is shown (idle state has no Stop button) and re-packed in on_connected.
        self.stop_btn.pack(pady=(6, 14))

        # Measure the base layout (no approval prompt / no banner yet) and use it
        # as the minimum size, so the base content can never be shrunk into a
        # clip. The window still grows to fit the transient prompt/banner via
        # _refit() on each state change.
        self.root.update_idletasks()
        base_w = self.root.winfo_reqwidth()
        base_h = self.root.winfo_reqheight()
        # Fixed, compact window (Option B): the connected banner is OVERLAID on top
        # of the content with place() (full-width via relwidth), so it adds no
        # height and needs no width contribution; the approval prompt is now its own
        # window. Lock to the base layout size with a sensible minimum width.
        fixed_w = max(base_w, 274)
        fixed_h = base_h
        self.root.geometry(f"{fixed_w}x{fixed_h}")
        self.root.minsize(fixed_w, fixed_h)
        self.root.maxsize(fixed_w, fixed_h)
        self.root.resizable(False, False)

        # Height of the top region (title + subtitle) so the connected banner can
        # be sized to fully cover both. Use requested heights (valid before the
        # window is mapped) plus the title's pack padding (18 top, 2 bottom).
        self.banner_cover_h = (
            18 + self.title_label.winfo_reqheight() + 2
            + self.subtitle.winfo_reqheight() + 6
        )

        # Base layout is measured; hide the Stop button so it only appears while a
        # viewer is connected (its height stays reserved in the fixed window).
        self.stop_btn.pack_forget()

    def _build_theme_selector(self):
        # A modern segmented control of rounded buttons (matching the app's other
        # buttons): the active choice is highlighted in brand, the rest are neutral.
        row = tk.Frame(self.root, bg=BG)
        tk.Label(row, text=tr("theme.label") + ":", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        seg = tk.Frame(row, bg=BG)
        seg.pack(side="left")
        sfont = tkfont.Font(family="Segoe UI Semibold", size=9)
        labels = {"auto": tr("theme.auto"), "light": tr("theme.light"),
                  "dark": tr("theme.dark")}
        for choice in ("auto", "light", "dark"):
            active = choice == self._theme_choice
            RoundedButton(
                seg, labels[choice],
                BRAND if active else NEUTRAL_BG,
                COPY_FG if active else NEUTRAL_FG,
                font=sfont, parent_bg=BG, min_width=0, pad_x=12, pad_y=6,
                radius=9, border=(None if active else BORDER),
                command=lambda c=choice: self._on_theme_pick(c),
            ).pack(side="left", padx=2)
        row.pack(pady=(0, 10))

    def _on_theme_pick(self, choice):
        if choice == self._theme_choice:
            return
        # Defer so the clicked button isn't destroyed while handling its own event.
        self.root.after(10, lambda: self.retheme(choice))

    def _status_color(self, role):
        return {"good": GOOD, "bad": BAD, "warn": WARN}.get(role, MUTED)

    def retheme(self, choice):
        """Switch theme live by re-pointing the color globals and rebuilding the UI,
        preserving the current code / status / connected state."""
        self._theme_choice = choice
        _save_theme_choice(choice)
        _apply_theme(_resolve_theme(choice))
        code, status_text, role = self._code, self._status_text, self._status_role
        auto, connected = self.auto_var.get(), self._connected
        for aid in list(self._anims.values()):
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
        self._anims.clear()
        for w in self.root.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass
        self.auto_accept = auto
        self._build_ui()
        if code:
            self.code_panel.set_text(code)
        if status_text:
            self.set_status(status_text, self._status_color(role))
        if connected:
            # Re-show the connected UI at full color (no fade on a theme switch).
            self.stop_btn.pack(pady=(6, 14))
            self.stop_btn.set_fade(1.0)
            self.banner.set_text(tr("agent.banner.controlling"))
            self.banner.set_fade_colors(BANNER_BG, BANNER_FG)
            self.banner.place(x=0, y=0, relwidth=1.0, height=self.banner_cover_h)
            tk.Misc.tkraise(self.banner)
            self.banner.update_idletasks()

    def _refit(self):
        """No-op: the window is a fixed size (sized up-front for every state),
        so it must not resize when the approval prompt or banner toggles."""
        return

    # ---- thread-safe UI updates ----
    def post(self, fn):
        self.q.put(fn)

    def _drain(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self._update_window_rects()  # keep our window rects / focus state fresh
        self._drain_after = self.root.after(80, self._drain)

    def _update_window_rects(self):
        """Refresh the on-screen rects of ALL our windows (main window + any open
        dialog, full frames incl. the title bar, in physical screen coords) and
        whether one of them currently has OS focus. apply_event() (network thread)
        reads these to ignore injected clicks on us and injected keys while we are
        focused. Runs on the Tk thread (GetWindowRect must not run off it)."""
        rects = []
        try:
            import ctypes
            from ctypes import wintypes

            u = ctypes.windll.user32
            our_hwnds = set()

            def _rect_for(widget):
                try:
                    hwnd = u.GetAncestor(widget.winfo_id(), 2)  # GA_ROOT
                    if not hwnd:
                        return None
                    r = wintypes.RECT()
                    if u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
                        our_hwnds.add(hwnd)
                        return (r.left, r.top, r.right, r.bottom)
                except Exception:
                    pass
                return None

            main = _rect_for(self.root)
            if main:
                rects.append(main)
            for w in self.root.winfo_children():  # ActionDialog Toplevels
                if isinstance(w, tk.Toplevel):
                    try:
                        if w.winfo_exists() and w.winfo_viewable():
                            dr = _rect_for(w)
                            if dr:
                                rects.append(dr)
                    except Exception:
                        pass
            self._agent_foreground = u.GetForegroundWindow() in our_hwnds
        except Exception:
            pass
        if rects:
            self._window_rects = tuple(rects)
        elif not self._window_rects:
            # No rect yet and the API failed: fall back to Tk client bounds so some
            # protection exists (misses the title bar, but better than nothing).
            try:
                x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
                self._window_rects = ((x, y, x + self.root.winfo_width(),
                                       y + self.root.winfo_height()),)
            except Exception:
                pass
        # else: keep the last good rects rather than clearing them.

    # ---- fade animations (color interpolation; Tk has no per-widget alpha) ----
    def _animate(self, key, draw, duration=170, steps=12, on_done=None):
        """Run draw(t) for t stepping 0..1 over `duration` ms. A new animation with
        the same key cancels the previous one (so fade-in/out never fight)."""
        prev = self._anims.pop(key, None)
        if prev is not None:
            try:
                self.root.after_cancel(prev)
            except Exception:
                pass
        interval = max(1, int(duration / steps))

        def tick(i):
            try:
                draw(i / steps)
            except Exception:
                pass
            if i < steps:
                self._anims[key] = self.root.after(interval, lambda: tick(i + 1))
            else:
                self._anims.pop(key, None)
                if on_done:
                    try:
                        on_done()
                    except Exception:
                        pass

        tick(0)

    def _fade_banner(self, show):
        def draw(t):
            tt = t if show else 1 - t
            self.banner.set_fade_colors(_lerp_hex(BG, BANNER_BG, tt),
                                        _lerp_hex(BG, BANNER_FG, tt))

        def done():
            if not show:
                self.banner.place_forget()

        self._animate("banner", draw, on_done=done)

    def _fade_stop(self, show):
        def draw(t):
            self.stop_btn.set_fade(t if show else 1 - t)

        def done():
            if not show:
                self.stop_btn.pack_forget()

        self._animate("stop", draw, on_done=done)

    # ---- callbacks from the network client ----
    def set_status(self, text, color=None):
        if color is None:
            color = MUTED
        self.status.config(text=text, fg=color)
        # Remember text + role so the status survives a live theme switch (the color
        # value changes with the theme, the role doesn't).
        self._status_text = text
        self._status_role = {GOOD: "good", BAD: "bad", WARN: "warn"}.get(color, "muted")

    def on_code(self, code, http_base):
        self._code = code
        self._http_base = http_base
        self.code_panel.set_text(code)
        self.url_panel.set_text(tr("agent.connectAt") + "\n" + http_base + "/connect")

    def show_approval(self, ev, box):
        # The consent prompt is its own window now. The decision fires immediately
        # (unblocking the network thread), then the window fades out.
        def decide(ok):
            box["ok"] = ok
            ev.set()

        self.set_status(tr("agent.status.asking"), WARN)
        self._approval_dialog = ActionDialog(
            self.root, tr("agent.approval.prompt"),
            tr("agent.btn.approve"), GOOD, APPROVE_FG,
            tr("agent.btn.deny"), BAD, DENY_FG,
            on_pos=lambda: decide(True), on_neg=lambda: decide(False),
        )

    def _close_approval_dialog(self):
        """Dismiss a still-open approval window (e.g. the viewer left first)."""
        dlg = getattr(self, "_approval_dialog", None)
        if dlg is not None:
            self._approval_dialog = None
            try:
                if dlg.winfo_exists() and not dlg._done:
                    dlg._choose_neg()
            except Exception:
                pass

    def on_connected(self):
        self._connected = True
        self.set_status(tr("agent.status.connected"), GOOD)
        # Show the Stop button only while a viewer is connected (packed back into
        # the normal flow, below the checkbox). Room for it is reserved in the
        # fixed window height. It fades in from the background.
        self.stop_btn.pack(pady=(6, 14))
        self._fade_stop(True)
        # Overlay the banner at the TOP of the window as a full-width strip that
        # covers the title AND the "This computer can be shared" subtitle; fade it in.
        self.banner.set_text(tr("agent.banner.controlling"))
        self.banner.set_fade_colors(BG, BG)  # start blended with the background
        self.banner.place(x=0, y=0, relwidth=1.0, height=self.banner_cover_h)
        tk.Misc.tkraise(self.banner)  # window-stacking raise (Canvas.tkraise is tag_raise)
        self.banner.update_idletasks()
        self._fade_banner(True)

    def on_denied(self):
        self._connected = False
        self.set_status(tr("agent.status.denied"), WARN)

    def on_peer_left(self, local=False):
        # local=True  -> we ended the session (Stop button / window): "you closed it"
        # local=False -> the viewer disconnected on their own.
        self._connected = False
        self._close_approval_dialog()  # e.g. the viewer left before we decided
        self._fade_banner(False)
        self._fade_stop(False)
        self.set_status(
            tr("agent.status.ended") if local else tr("agent.status.viewerLeft"),
            WARN,
        )

    def on_fatal(self, message):
        self.set_status(tr("agent.error.couldNotStart"), BAD)
        messagebox.showerror("Audiobus Remote", message)

    def copy_code(self):
        code = self.code_panel.get_text()
        if code and "·" not in code:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.copy_btn.set_text(tr("agent.btn.copied"))
            self.root.after(1500, lambda: self.copy_btn.set_text(tr("agent.btn.copy")))

    def _toggle_auto(self):
        self.auto_accept = self.auto_var.get()

    def end_session_click(self):
        # "Stop session": confirm first in a separate window; only end the session
        # if the user agrees. (Closing the main window still quits via on_close.)
        ActionDialog(
            self.root, tr("agent.confirm.stop"),
            tr("agent.btn.stop"), STOP_BG, STOP_FG,
            tr("ui.cancel"), NEUTRAL_BG, NEUTRAL_FG,
            on_pos=self._do_end_session, on_neg=None,
        )

    def _do_end_session(self):
        # End the current session only — stop streaming and tell the viewer — but
        # keep the program running and connected, ready for a new connection.
        self._connected = False
        if self.client:
            self.client.end_session()
        self._fade_banner(False)
        self._fade_stop(False)
        self.set_status(tr("agent.status.ended"), WARN)

    def on_close(self):
        # Closing the window quits the program (and ends the session). While a viewer
        # is connected, confirm first (same warning as "Stop session") so an
        # accidental close doesn't drop a live session.
        if not self._connected:
            self._quit()
            return
        if getattr(self, "_close_pending", False):
            return
        self._close_pending = True

        def on_yes():
            self._close_pending = False
            self._quit()

        def on_no():
            self._close_pending = False

        ActionDialog(
            self.root, tr("agent.confirm.stop"),
            tr("agent.btn.stop"), STOP_BG, STOP_FG,
            tr("ui.cancel"), NEUTRAL_BG, NEUTRAL_FG,
            on_pos=on_yes, on_neg=on_no,
        )

    def _quit(self):
        if self.client:
            self.client.request_stop()
        self.root.after(250, self._finish)

    def _finish(self):
        # Cancel in-flight fade animations and the drain loop so their after
        # callbacks don't fire against destroyed widgets during teardown.
        for aid in list(self._anims.values()) + [self._drain_after]:
            try:
                if aid is not None:
                    self.root.after_cancel(aid)
            except Exception:
                pass
        self._anims.clear()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.client = NetClient(self, self.args)
        self.client.start()
        self.root.mainloop()


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Audiobus Remote agent — share this computer with a session code."
    )
    p.add_argument(
        "--server",
        default=os.environ.get("AUDIOBUS_SERVER", DEFAULT_SERVER),
        help="Audiobus server URL (default: %(default)s or $AUDIOBUS_SERVER).",
    )
    p.add_argument("--fps", type=int, default=20, help="Target frames per second.")
    p.add_argument("--bitrate", type=int, default=None,
                   help="Best-effort max video bitrate in kbps (default: adaptive).")
    p.add_argument("--scale", type=float, default=None, help="Fixed downscale (0-1).")
    p.add_argument("--max-width", type=int, default=1920,
                   help="Auto-downscale so frames are at most this wide.")
    p.add_argument("--monitor", type=int, default=1,
                   help="Monitor index (1 = primary, 0 = all).")
    p.add_argument("--auto-accept", action="store_true",
                   help="Start with the approval prompt disabled.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        App(args).run()
    except Exception as e:
        try:
            messagebox.showerror("Audiobus Remote", str(e))
        except Exception:
            sys.stderr.write(str(e) + "\n")
    finally:
        os._exit(0)  # ensure the daemon network thread and executors die


if __name__ == "__main__":
    main()
