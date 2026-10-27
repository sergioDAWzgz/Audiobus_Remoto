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
import base64
import ctypes
from ctypes import wintypes
import fractions
import io
import json
import os
import queue
import secrets
import ssl
import sys
import threading
import time
import urllib.request

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox

try:
    import websockets
    try:
        from mss import MSS as ScreenGrabber  # mss >= 10
    except ImportError:
        from mss import mss as ScreenGrabber  # mss < 10
    from PIL import Image, ImageGrab
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

# Shown in place of the real session code while the agent has no link to the server
# (e.g. internet lost) and no active session: it makes clear the code is not usable
# right now. The real code is restored once the link comes back. Uses the same dotted
# placeholder the panel starts with before a code is assigned (never a valid code).
CODE_PLACEHOLDER = "·" * 10

# A plain product User-Agent. The default "Python-urllib/..." UA is blocked by
# Cloudflare's bot filtering, so we must send our own on every request.
USER_AGENT = "AudiobusRemoteAgent/0.1"

# Connection heartbeat / reconnection tuning.
HB_PING_SECS = 1.0          # send an app-level "ping" this often
HB_WOBBLE_SECS = 1.5        # no "pong" for this long -> flag the link as interrupted
#                            IMMEDIATELY (status goes yellow) from the first missed check,
#                            before the full-drop handling; reverts if the pong resumes
HB_INTERRUPT_SECS = 3.0     # no "pong" for this long -> the server link is interrupted
#                            (close + reconnect, and schedule the Wait/Cut dialog)
HB_RECONNECT_SECS = 1.0     # wait between reconnect attempts (after the first, immediate one)
#                            -> the agent reconnects fast once the internet comes back
HB_CONNECT_TIMEOUT_SECS = 4.0  # cap each (re)connect handshake so a half-open/unreachable
#                                server fails fast instead of hanging ~10s per attempt
HB_WARN_SECS = 2.0          # reconnect starts at once; show the Wait/Cut dialog this long
#                            after the drop is detected -> ~5s total (3s detect + 2s)
HB_MAX_RECONNECT_SECS = 65.0  # > the DO's 60s grace: give up reconnecting after this
INTERRUPT_REASK_MS = 10000  # re-ask the Wait/Cut dialog this long after "Wait"

# Clipboard sync + file transfer tuning.
CLIP_POLL_MS = 500             # how often the GUI thread polls the OS clipboard for changes
CLIP_MAX_BYTES = 256 * 1024    # cap on synced clipboard text (UTF-8); larger is skipped
CLIP_IMG_MAX_BYTES = 16 * 1024 * 1024  # cap on a synced clipboard image (PNG); larger skipped
FILE_CHUNK_BYTES = 128 * 1024  # raw bytes per file-chunk (base64 inflates ~33% on the wire)
FILE_MAX_BYTES = 2 * 1024 * 1024 * 1024  # hard cap per received/sent file (2 GiB)
FILE_WINDOW = 8                # unacked file-chunks allowed in flight (sender flow control)

# Auto-accept PIN. When the owner turns on auto-accept, a short numeric PIN is generated
# and shown in the agent; a fresh viewer must present it to be admitted silently. This
# keeps a *leaked session code alone* from granting silent access — the code-holder also
# needs the out-of-band PIN. Brute force is bounded: PIN_LOCKOUT cumulative wrong answers
# refuse all challenges for a cooldown that DOUBLES on every repeat lockout (up to
# PIN_COOLDOWN_MAX_S), so a reconnecting code-holder can't grind the space at a fixed rate
# (and still needs the code). The owner is alerted on each lockout. 6 digits (10^6) with
# escalating backoff makes a realistic brute-force attempt take months.
PIN_LEN = 6                    # digits in the generated PIN (short, shared out-of-band)
PIN_LOCKOUT = 10               # cumulative wrong PINs before challenges are refused...
PIN_COOLDOWN_S = 60            # ...for this base cooldown, doubling on each repeat lockout...
PIN_COOLDOWN_MAX_S = 3600      # ...up to this cap (1 hour)
PIN_TIMEOUT_S = 90             # whole-challenge deadline: a viewer must finish within this


def _new_pin():
    """A fresh, cryptographically-random numeric PIN of PIN_LEN digits (zero-padded)."""
    return f"{secrets.randbelow(10 ** PIN_LEN):0{PIN_LEN}d}"

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


def _subtract_rect(rects, c):
    """Subtract rect c (l,t,r,b) from each rect in `rects`, returning the remaining pieces
    (up to four per input rect). Pure geometry; used to carve covering windows out of the
    agent's frame so only its visible parts remain."""
    cl, ct, cr, cb = c
    out = []
    for (l, t, r, b) in rects:
        xl, yt, xr, yb = max(l, cl), max(t, ct), min(r, cr), min(b, cb)
        if xr <= xl or yb <= yt:
            out.append((l, t, r, b))      # no real overlap -> keep whole
            continue
        if yt > t:
            out.append((l, t, r, yt))     # strip above the cover
        if yb < b:
            out.append((l, yb, r, b))     # strip below the cover
        if xl > l:
            out.append((l, yt, xl, yb))   # strip left of the cover
        if xr < r:
            out.append((xr, yt, r, yb))   # strip right of the cover
    return out


def _visible_subrects(full_rect, higher_rects):
    """The parts of full_rect not covered by any of higher_rects (windows above it)."""
    vis = [full_rect]
    for c in higher_rects:
        vis = _subtract_rect(vis, c)
        if not vis:
            break
    return vis


def _window_title(hwnd):
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(160)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 160)
        return buf.value
    except Exception:
        return ""


# Opt-in click-filter diagnostics: set AUDIOBUS_DEBUG_CLICKS=1 to append each click's
# decision to ~/audiobus-click.log. Off (and zero-cost) by default.
_CLICK_DEBUG = bool(os.environ.get("AUDIOBUS_DEBUG_CLICKS"))
_NET_DPI_LOGGED = False   # one-time net-thread DPI-awareness diagnostic
_GUI_DPI_LOGGED = False   # one-time GUI-thread DPI-awareness + raw-rect diagnostic
_SHOT_SAVED = False       # one-time: save an mss screenshot on the first blocked click


_AGENT_BUILD = "2026-10-27-nohover"  # bumped per diagnostic build; appears in the click log


def _save_debug_shot(x, y):
    """One-time: save an mss screenshot (exactly what the stream shows) plus the agent's
    window rect, so a blocked click the viewer disputes can be checked visually — where the
    agent actually appears in the capture vs the rect the hit-test uses. Writes
    ~/audiobus-shot.png."""
    global _SHOT_SAVED
    if _SHOT_SAVED:
        return
    _SHOT_SAVED = True
    try:
        import mss.tools
        with ScreenGrabber() as sct:
            img = sct.grab(sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
        path = os.path.join(os.path.expanduser("~"), "audiobus-shot.png")
        mss.tools.to_png(img.rgb, img.size, output=path)
        _click_log(f"[diag saved screenshot {path} size={img.size} for blocked click ({x},{y})]")
    except Exception as e:
        _click_log(f"[diag screenshot failed: {e!r}]")


def _click_log(msg):
    try:
        with open(os.path.join(os.path.expanduser("~"), "audiobus-click.log"),
                  "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _dpi_awareness_name():
    """The CURRENT thread's DPI awareness, for diagnostics (Win10 1607+)."""
    try:
        u = ctypes.windll.user32
        u.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        u.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        u.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        ctx = u.GetThreadDpiAwarenessContext()
        val = u.GetAwarenessFromDpiAwarenessContext(ctx)
        return {0: "UNAWARE", 1: "SYSTEM", 2: "PER_MONITOR",
                3: "PER_MONITOR_V2", 4: "UNAWARE_GDISCALED"}.get(val, f"val={val}")
    except Exception as e:
        return f"?({e!r})"


def _screen_pixel(x, y):
    """(r,g,b) of the COMPOSITED desktop at physical (x,y) — i.e. what is VISIBLY on screen
    (and in the capture/stream) there. Lets a blocked-click log say whether the agent window
    is actually visible at that point, or the app behind it shows through (which would mean
    the window is in the hit-test but not the capture)."""
    try:
        u = ctypes.windll.user32
        g = ctypes.windll.gdi32
        g.GetPixel.restype = ctypes.c_uint32
        g.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        hdc = u.GetDC(0)
        c = g.GetPixel(ctypes.c_void_p(hdc), int(x), int(y))
        u.ReleaseDC(0, hdc)
        if c == 0xFFFFFFFF:  # CLR_INVALID
            return None
        return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)
    except Exception:
        return None


def _win_diag(hwnd):
    """root hwnd / title / rect of hwnd, for diagnosing what WindowFromPoint returned."""
    try:
        u = ctypes.windll.user32
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        root = u.GetAncestor(hwnd, 2)  # GA_ROOT
        r = wintypes.RECT()
        u.GetWindowRect(wintypes.HWND(root), ctypes.byref(r))
        return f"root={root} title={_window_title(root)!r} rect=({r.left},{r.top},{r.right},{r.bottom})"
    except Exception as e:
        return f"?({e!r})"


def _window_owner_pid(hwnd):
    """Process id that owns hwnd, or 0."""
    import ctypes
    from ctypes import wintypes
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _click_on_agent(x, y, rects):
    """True if an injected click at absolute (x, y) should be blocked because it lands on
    one of the agent's OWN windows AND that window is really the top-most one there.

    Fast reject: outside every agent rect -> not on the agent. Inside a rect, another
    window may now be covering the agent at that point, so ask the OS which window is
    actually at that point (Z-order aware, via WindowFromPoint) and block only when that
    window belongs to THIS process. Identifying our window by owning process id (rather
    than a cached handle) is robust to Tk's wrapper-window structure on Windows, where the
    decorated OS window's handle differs from GetAncestor(winfo_id, GA_ROOT) — a mismatch
    that made the handle-set check never match. If the check can't run, fall back to
    blocking so protection is never silently weakened. Safe to call off the Tk thread (the
    Win32 hit-test is a read-only query)."""
    if not _in_rects(x, y, rects):
        if _CLICK_DEBUG:
            _click_log(f"({x},{y}) outside agent rects {list(rects or ())} -> allow")
        return False
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        u.WindowFromPoint.argtypes = [wintypes.POINT]  # POINT is passed BY VALUE
        u.WindowFromPoint.restype = ctypes.c_void_p    # HWND is 64-bit; don't truncate it
        global _NET_DPI_LOGGED
        if _CLICK_DEBUG and not _NET_DPI_LOGGED:
            _NET_DPI_LOGGED = True
            _click_log(f"[diag net-thread DPI={_dpi_awareness_name()}]")
        hwnd = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            if _CLICK_DEBUG:
                _click_log(f"({x},{y}) in rect; no window at point -> allow")
            return False  # nothing is there to protect
        pid = _window_owner_pid(hwnd)
        on_agent = (pid == os.getpid())
        if _CLICK_DEBUG:
            _click_log(f"({x},{y}) in rect; WindowFromPoint hwnd={hwnd} pid={pid} "
                       f"title={_window_title(hwnd)!r} our_pid={os.getpid()} "
                       f"screen_px={_screen_pixel(x, y)} [{_win_diag(hwnd)}] -> "
                       f"{'BLOCK' if on_agent else 'allow'}")
            if on_agent:
                _save_debug_shot(x, y)  # one-time: capture what the stream shows here
        return on_agent
    except Exception as e:
        if _CLICK_DEBUG:
            _click_log(f"({x},{y}) check failed ({e!r}) -> BLOCK (fail-closed)")
        return True  # fail closed: keep the original protective behaviour


def apply_event(ev, monitors=None, window_rects=None, blocked=None,
                pressed=None, agent_foreground=False):
    """Apply one remote input event, while protecting the agent's OWN windows so a
    remote user can't close it or drive its controls:

    - Mouse CLICKS/SCROLL that land on an agent window AND where that window is actually
      the top-most one (checked Z-order aware via the owning process, so a click over
      another window that now covers the agent is NOT blocked) are ignored. Cursor
      MOVEMENT may still pass over them.
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
            ax, ay = off_x + ev["x"], off_y + ev["y"]
            # Don't glide the real cursor ONTO an agent window: it would hover/highlight our
            # own title-bar buttons (minimize/maximize/close) — a "phantom click" the viewer
            # sees even though the actual click is refused. The viewer already draws a blocked
            # cursor over us, so freezing the OS cursor at our edge keeps the two consistent.
            if not _click_on_agent(ax, ay, window_rects):
                mouse.position = (ax, ay)
        elif t == "md":
            ax, ay = off_x + ev["x"], off_y + ev["y"]
            b = ev.get("b", 0)
            if _click_on_agent(ax, ay, window_rects):
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
            if _click_on_agent(ax, ay, window_rects):
                return  # don't scroll an agent window that is actually on top here
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


def _force_close_ice_sockets(pc):
    """Close a peer connection's underlying UDP sockets directly, reaching into aiortc's
    internals (best-effort). On a whole-network drop, aiortc's pc.close() can block the
    event loop for a long time waiting on sockets that are open but unreachable; dropping
    the sockets first makes close() return promptly so our net thread keeps detecting the
    drop and reconnecting. Private attribute access is guarded — if aiortc changes, this
    silently does nothing and we fall back to the bounded close()."""
    try:
        ice_transports = getattr(pc, "_RTCPeerConnection__iceTransports", None) or []
        for ice in ice_transports:
            conn = getattr(ice, "_connection", None)
            for proto in list(getattr(conn, "_protocols", []) or []):
                tr = getattr(proto, "transport", None) or getattr(proto, "_transport", None)
                if tr is not None:
                    try:
                        tr.close()
                    except Exception:
                        pass
    except Exception:
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

def _clipboard_seq():
    """The Windows clipboard sequence number (bumps on every clipboard change), or None.
    Used to detect changes cheaply and to echo-guard our own writes (text and images)."""
    try:
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:
        return None


def _encode_png(img):
    """PIL image -> PNG bytes (RGBA/RGB preserved)."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _set_clipboard_image(img):
    """Put a PIL image on the Windows clipboard as CF_DIB (no pywin32 needed). Handles are
    64-bit, so every alloc/lock call sets restype/argtypes to avoid pointer truncation."""
    bmp = io.BytesIO()
    img.convert("RGB").save(bmp, format="BMP")
    data = bmp.getvalue()[14:]  # DIB = BMP minus the 14-byte BITMAPFILEHEADER
    k = ctypes.windll.kernel32
    u = ctypes.windll.user32
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalFree.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    GMEM_MOVEABLE = 0x0002
    CF_DIB = 8
    h = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = k.GlobalLock(h)
    if not p:
        k.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    k.GlobalUnlock(h)
    if not u.OpenClipboard(None):
        k.GlobalFree(h)
        return False
    try:
        u.EmptyClipboard()
        if not u.SetClipboardData(CF_DIB, h):
            k.GlobalFree(h)  # ownership stays with us on failure
            return False
        return True  # on success the system owns the handle; don't free it
    finally:
        u.CloseClipboard()


def received_files_dir():
    """Where incoming files land: ~/Downloads/AudiobusRemote (created on demand)."""
    base = os.path.join(os.path.expanduser("~"), "Downloads", "AudiobusRemote")
    os.makedirs(base, exist_ok=True)
    return base


def safe_filename(name):
    """Reduce a viewer-supplied name to a bare, safe basename (no path traversal)."""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = "".join(c for c in name if c not in '<>:"/\\|?*' and ord(c) >= 32)
    name = name.strip().strip(".")
    return name[:200] or "file"


def dedupe_path(directory, name):
    """A non-colliding full path in directory for name (adds ' (1)', ' (2)', ...)."""
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = os.path.join(directory, f"{stem} ({i}){ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


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
        self._last_pong = 0.0       # time.time() of the last heartbeat "pong"
        self._viewer_approved = False  # the owner approved the currently-held viewer
        self._approved_vid = None   # the server's id for the viewer socket the owner
        #                             approved; a resumed re-serve is auto-admitted only
        #                             if its vid matches, so a different code-holder who
        #                             took over the slot can't inherit the approval
        self._approved_vtok = None  # the VIEWER's own stable resume token (minted by the
        #                             viewer's browser, carried in every (re)connect). The
        #                             vid identifies a socket and changes when the viewer
        #                             reconnects; the vtok identifies the approved viewer
        #                             ACROSS reconnects, so the same approved browser can
        #                             resume a dropped session WITHOUT re-prompting, while a
        #                             different code-holder (a different, unguessable vtok)
        #                             still earns consent. Kept across a transient peer-left
        #                             (the drop may be recovering); cleared on an explicit
        #                             local end so a later join re-consents.
        self._ended = False         # owner ended the session locally; a held viewer the
        #                             server re-serves on reconnect must be dismissed,
        #                             not resumed (survives a dead-socket send failure)
        self._link_down = False     # our link to the server is currently interrupted,
        #                             so a pending approval is for a connection that's gone
        self._left_reported = False # viewer-gone reported once per session (dedup)
        # Serialize _on_viewer: each viewer-joined spawns a task, and two can arrive
        # close together (a flapping viewer the server replaces twice). The lock (created
        # on the net-thread loop in _run) keeps their teardown + re-serve from
        # interleaving and corrupting pc/capture/streaming; the generation counter lets a
        # newer join supersede an older one still queued.
        self._viewer_lock = None
        self._viewer_gen = 0
        self._blocked_btns = set()  # buttons whose press was suppressed over our window
        self._pressed_btns = set()  # buttons we actually injected a press for (to release)
        self._last_blockrects = None  # last blockrects JSON sent to the viewer (dedup)
        # True between "we ended the session locally" and the resulting peer-left,
        # so the status reads "you closed the connection" rather than blaming the
        # viewer for a disconnect we initiated.
        self.ended_locally = False
        # File transfer. Incoming (viewer->host): id -> {"f","path","name","size","got"}.
        # Outgoing (host->viewer): id -> {"acked": int, "event": asyncio.Event, "cancel": bool}.
        self._recv_files = {}
        self._send_files = {}
        self._clip_img_rx = None  # in-flight incoming clipboard image: {"id","buf"}
        self._clip_img_lock = None  # serializes outgoing clipboard-image sends (net loop)
        # Auto-accept PIN gating (see _challenge_pin). A challenge runs inside _serve_viewer
        # while it holds the viewer lock, so a single queue/event/counter set is enough.
        self._pin_queue = None       # asyncio.Queue of a viewer's PIN answers during a
        #                              challenge; None when no challenge is in progress
        self._pin_cancel = None      # asyncio.Event: abort the active challenge (the viewer
        #                              left, or a newer viewer-joined superseded it)
        self._pin_superseded = False # the last challenge ended by cancel, not a real deny
        #                              (so _serve_viewer skips the spurious 'denied' send)
        self._pin_fails = 0          # cumulative wrong PINs in the current lockout window
        self._pin_backoff = 0        # repeat-lockout count -> exponential cooldown
        self._pin_lock_until = 0.0   # loop.time() until which challenges are refused

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
        self._ended = True             # authoritative across a reconnect (see _on_viewer):
        #                                even if the send below fails on a dead socket, a
        #                                held viewer re-served later must NOT be resumed
        self._viewer_approved = False  # session ended by us; a new viewer must consent
        await self._stop_stream()
        if self.ws:
            try:
                # Tell the viewer promptly so it drops out of the session; it then
                # closes, which frees the single-session slot for a new viewer.
                await self.ws.send(json.dumps({"type": "session-ended"}))
            except Exception:
                pass

    def send_clip(self, text):
        """Push the host clipboard to the viewer (called from the GUI thread when the
        local clipboard changes). Hops to the net-thread loop to do the actual send."""
        if self.loop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._send_clip(text))
                )
            except Exception:
                pass

    async def _send_clip(self, text):
        if self.ws and self.streaming:
            try:
                await self.ws.send(json.dumps({"type": "clip", "text": text}))
            except Exception:
                pass

    def send_clip_image(self, png_bytes):
        """Push a host clipboard image to the viewer (called from the GUI thread)."""
        if self.loop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._send_clip_image(png_bytes)))
            except Exception:
                pass

    async def _send_clip_image(self, png_bytes):
        # Chunked so a big image can't monopolise the socket; each await ws.send applies
        # backpressure, so the heartbeat ping still interleaves between chunks. The lock
        # serializes concurrent image sends so their begin/chunk/end frames never interleave
        # on the single socket (the viewer reassembles one image at a time).
        lock = self._clip_img_lock
        if lock is None:
            return
        async with lock:
            if not self.ws or not self.streaming:
                return
            cid = "ci" + os.urandom(6).hex()
            try:
                await self._send_json({"type": "clip-img-begin", "id": cid,
                                       "size": len(png_bytes)})
                for n, off in enumerate(range(0, len(png_bytes), FILE_CHUNK_BYTES)):
                    if not self.streaming:
                        return
                    chunk = png_bytes[off:off + FILE_CHUNK_BYTES]
                    await self._send_json({"type": "clip-img-chunk", "id": cid, "n": n,
                                           "data": base64.b64encode(chunk).decode("ascii")})
                await self._send_json({"type": "clip-img-end", "id": cid})
            except Exception:
                pass

    # ---- File transfer (both directions ride the JSON relay as base64 chunks) ----

    async def _send_json(self, obj):
        if self.ws:
            try:
                await self.ws.send(json.dumps(obj))
            except Exception:
                pass

    async def _on_file_begin(self, msg):
        """Viewer->host: open a destination file and start accepting chunks."""
        fid = msg.get("id")
        if not isinstance(fid, str) or fid in self._recv_files:
            return
        name = safe_filename(msg.get("name"))
        try:
            size = int(msg.get("size", 0))
        except (TypeError, ValueError):
            size = -1
        if size < 0 or size > FILE_MAX_BYTES:
            await self._send_json({"type": "file-cancel", "id": fid, "reason": "too-large"})
            return
        try:
            directory = await self.loop.run_in_executor(None, received_files_dir)
            path = await self.loop.run_in_executor(None, dedupe_path, directory, name)
            f = await self.loop.run_in_executor(None, lambda: open(path, "wb"))
        except Exception:
            await self._send_json({"type": "file-cancel", "id": fid, "reason": "open-failed"})
            return
        base = os.path.basename(path)
        self._recv_files[fid] = {"f": f, "path": path, "name": base, "size": size, "got": 0}
        self.app.post(lambda: self.app.on_file_progress(base, 0, size, False))

    async def _on_file_chunk(self, msg):
        fid = msg.get("id")
        st = self._recv_files.get(fid)
        if not st:
            return
        data = msg.get("data")
        if not isinstance(data, str):
            return
        try:
            raw = base64.b64decode(data)
        except Exception:
            self._cancel_recv(fid)
            await self._send_json({"type": "file-cancel", "id": fid, "reason": "bad-data"})
            return
        if st["got"] + len(raw) > FILE_MAX_BYTES:
            self._cancel_recv(fid)
            await self._send_json({"type": "file-cancel", "id": fid, "reason": "too-large"})
            return
        try:
            await self.loop.run_in_executor(None, st["f"].write, raw)
        except Exception:
            self._cancel_recv(fid)
            await self._send_json({"type": "file-cancel", "id": fid, "reason": "write-failed"})
            return
        st["got"] += len(raw)
        await self._send_json({"type": "file-ack", "id": fid, "n": msg.get("n")})
        self.app.post(
            lambda s=st: self.app.on_file_progress(s["name"], s["got"], s["size"], False))

    async def _on_file_end(self, msg):
        fid = msg.get("id")
        st = self._recv_files.pop(fid, None)
        if not st:
            return
        try:
            await self.loop.run_in_executor(None, st["f"].close)
        except Exception:
            pass
        self.app.post(lambda s=st: self.app.on_file_done(s["name"], s["path"], False, True))

    def _cancel_recv(self, fid, notify=True):
        """Abort an incoming transfer: close + discard the partial file. With notify, also
        tell the UI it failed; a peer-initiated cancel passes notify=False (the viewer
        already knows, and this avoids a duplicate when we triggered the cancel ourselves)."""
        st = self._recv_files.pop(fid, None)
        if not st:
            return
        try:
            st["f"].close()
        except Exception:
            pass
        try:
            os.remove(st["path"])
        except Exception:
            pass
        if notify:
            self.app.post(lambda s=st: self.app.on_file_done(s["name"], s["path"], False, False))

    def send_file(self, path):
        """Host->viewer: start sending a local file (called from the GUI thread)."""
        if self.loop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._send_file(path)))
            except Exception:
                pass

    async def _send_file(self, path):
        if not self.ws or not self.streaming:
            return
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except Exception:
            self.app.post(lambda: self.app.on_file_done(name, path, True, False))
            return
        if size > FILE_MAX_BYTES:
            self.app.post(lambda: self.app.on_file_done(name, path, True, False))
            return
        fid = "h" + os.urandom(8).hex()
        st = {"acked": -1, "event": asyncio.Event(), "cancel": False}
        self._send_files[fid] = st
        try:
            await self._send_json({"type": "file-begin", "id": fid, "name": name,
                                   "size": size, "mime": ""})
            f = await self.loop.run_in_executor(None, lambda: open(path, "rb"))
            n = sent = 0
            try:
                while not st["cancel"]:
                    chunk = await self.loop.run_in_executor(None, f.read, FILE_CHUNK_BYTES)
                    if not chunk:
                        break
                    # Flow control: never keep more than FILE_WINDOW chunks unacked, so a
                    # slow viewer can't make us flood the relay (which would starve pings).
                    while not st["cancel"] and n - st["acked"] > FILE_WINDOW:
                        st["event"].clear()
                        if n - st["acked"] <= FILE_WINDOW:
                            break
                        try:
                            await asyncio.wait_for(st["event"].wait(),
                                                   timeout=HB_MAX_RECONNECT_SECS)
                        except asyncio.TimeoutError:
                            st["cancel"] = True
                    if st["cancel"]:
                        break
                    b64 = base64.b64encode(chunk).decode("ascii")
                    await self._send_json({"type": "file-chunk", "id": fid,
                                           "n": n, "data": b64})
                    sent += len(chunk)
                    n += 1
                    self.app.post(
                        lambda s=sent: self.app.on_file_progress(name, s, size, True))
            finally:
                await self.loop.run_in_executor(None, f.close)
            if st["cancel"]:
                self.app.post(lambda: self.app.on_file_done(name, path, True, False))
            else:
                await self._send_json({"type": "file-end", "id": fid})
                self.app.post(lambda: self.app.on_file_done(name, path, True, True))
        except Exception:
            self.app.post(lambda: self.app.on_file_done(name, path, True, False))
        finally:
            self._send_files.pop(fid, None)

    def _reset_transfers(self):
        """Drop all in-flight transfers (session ended / viewer left)."""
        for fid in list(self._recv_files.keys()):
            self._cancel_recv(fid)
        for st in self._send_files.values():
            st["cancel"] = True
            st["event"].set()
        self._clip_img_rx = None  # discard a partial incoming clipboard image

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as e:
            self.app.post(lambda: self.app.set_status(f"Error: {e}", BAD))

    async def _run(self):
        self.loop = asyncio.get_event_loop()
        self.stop_event = asyncio.Event()
        # Create the viewer-admission lock on THIS (net-thread) loop, like stop_event.
        self._viewer_lock = asyncio.Lock()
        self._clip_img_lock = asyncio.Lock()  # one clipboard-image send in flight at a time

        # NOTE on coordinates: the process is made per-monitor DPI-aware up front, before
        # any window or grabber exists (see _set_dpi_awareness in main). So screen capture,
        # the reported monitor origins, pynput cursor positioning, and Win32 GetWindowRect
        # (used for the window-click filter) are ALL in the same PHYSICAL pixel space, on
        # every thread — which is why the filter's rects line up with the injected click
        # coordinates even on a scaled display. (mss would also flip DPI awareness as a
        # side effect, but only here on the net thread and after the GUI thread's windows
        # already existed, which split the coordinate spaces on scaled displays.)
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
        if _CLICK_DEBUG:
            try:
                aw = ctypes.c_int()
                ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(aw))
                pa = aw.value
            except Exception:
                pa = "?"
            _click_log(f"[diag session start: process_awareness={pa}(2=permon) "
                       f"mss_monitors={self.monitors}]")

        http_base, ws_base = normalize_server(self.args.server)
        self._http_base = http_base
        self.app.post(lambda: self.app.set_status(tr("agent.status.requesting"), MUTED))
        try:
            code = await self.loop.run_in_executor(
                None, lambda: request_session(http_base, self.ssl_ctx)
            )
        except Exception as e:
            # Capture the message NOW: `e` is cleared when this except block exits, so a
            # lambda that referenced it would raise NameError when the GUI thread runs it.
            detail = str(e)
            self.app.post(
                lambda: self.app.on_fatal(tr("agent.error.unreachable") + "\n" + detail)
            )
            return
        self.code = code
        self.app.post(lambda: self.app.on_code(code, http_base))

        ws_url = f"{ws_base}/ws/agent?code={code}"
        # Only use TLS for wss:// (a plaintext ws:// server — e.g. a local dev
        # worker — rejects an ssl context).
        ws_ssl = self.ssl_ctx if ws_url.startswith("wss://") else None

        # Reconnect loop: once the first connection is ESTABLISHED, an unexpected
        # drop no longer kills the agent — we reconnect IMMEDIATELY with the SAME code
        # (the server holds the session briefly) and warn with the Wait/Cut dialog only
        # if we're still down after a grace. If an ACTIVE session can't be restored
        # before the viewer's grace elapses, that viewer is gone, but we KEEP
        # reconnecting (now as idle) so the agent recovers and is ready for a new viewer
        # whenever the link returns — it never gives up while the program is running.
        first = True
        interrupted_since = None  # monotonic time the current interruption began
        notified = False          # on_link_interrupted was posted for this interruption
        session_lost = False      # active-session grace elapsed; dropped to idle, still retrying
        while not self.stop_event.is_set():
            code_expired = False   # this attempt hit a 404 (our reservation is gone)
            try:
                async with websockets.connect(
                    ws_url,
                    ssl=ws_ssl,
                    max_size=None,
                    ping_interval=20,
                    # First connect gets the library default (10s) so a slow-but-valid
                    # initial handshake isn't aborted; reconnect attempts fail fast so a
                    # half-open/unreachable server doesn't hang each retry ~10s.
                    open_timeout=(10.0 if first else HB_CONNECT_TIMEOUT_SECS),
                    # Force a half-open socket closed quickly instead of waiting the ~10s
                    # default for a graceful-close handshake that will never arrive.
                    close_timeout=HB_RECONNECT_SECS,
                    user_agent_header=USER_AGENT,
                ) as ws:
                    self.ws = ws
                    self._last_pong = time.time()
                    self._link_down = False  # (re)connected: our link is live again
                    if first:
                        first = False
                        self.app.post(
                            lambda: self.app.set_status(tr("agent.status.waiting"), WARN)
                        )
                    elif notified:
                        # Reconnected after we'd (scheduled/shown) the warning: dismiss
                        # it and restore. The server replays viewer-joined, so a held
                        # viewer is re-served through _on_viewer (resumed=True).
                        self.app.post(self.app.on_reconnected)
                    interrupted_since = None
                    notified = False
                    session_lost = False
                    recv = asyncio.create_task(self._recv_loop(ws))
                    hb = asyncio.create_task(self._heartbeat(ws))
                    stop = asyncio.create_task(self.stop_event.wait())
                    done, pending = await asyncio.wait(
                        {recv, hb, stop}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    # Retrieve the result/exception of EVERY task (the one that finished
                    # first and the cancelled ones). On a real half-open drop _recv_loop
                    # raises ConnectionClosedError as the socket breaks; if we never
                    # retrieve it, asyncio logs "Task exception was never retrieved" with a
                    # scary traceback. The drop itself is already handled (the heartbeat
                    # posts on_link_interrupted and the reconnect loop runs), so here we
                    # only drain these tasks to keep teardown clean and quiet.
                    for task in (*done, *pending):
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    if self.stop_event.is_set():
                        # Deliberate quit (window close / Cut): tell a connected viewer
                        # so it ends promptly instead of waiting out the server's grace.
                        try:
                            await ws.send(json.dumps({"type": "session-ended"}))
                        except Exception:
                            pass
                    await self._stop_stream()
            except Exception as e:
                # A 404 means the server IS reachable but our reservation is gone (a long
                # worker outage let it expire): note it so we re-reserve a fresh code
                # below. Anything else is a transient drop / unreachable server, handled
                # as an interruption (keep retrying the same code). The status lives at
                # e.response.status_code (websockets >= 14) or e.status_code (older).
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is None:
                    status = getattr(e, "status_code", None)
                code_expired = status == 404
            if self.stop_event.is_set():
                break
            if first:
                # Never established (the initial connect failed): keep today's
                # behavior — report the server is unreachable and give up.
                self.app.post(lambda: self.app.on_fatal(tr("agent.error.unreachable")))
                return
            if code_expired and not self.app._connected:
                # Idle and our code expired (very long outage): request a fresh code and
                # carry on with it, rather than retrying a dead code forever. (A short
                # outage keeps the SAME code — the server holds the reservation.)
                try:
                    new_code = await self.loop.run_in_executor(
                        None, lambda: request_session(http_base, self.ssl_ctx)
                    )
                except Exception:
                    new_code = None
                if new_code:
                    self.code = new_code
                    ws_url = f"{ws_base}/ws/agent?code={new_code}"
                    ws_ssl = self.ssl_ctx if ws_url.startswith("wss://") else None
                    interrupted_since = None
                    notified = False
                    self.app.post(lambda c=new_code: self.app.on_code(c, http_base))
                    self.app.post(self.app.on_reconnected)
                    continue
                # Still unreachable: fall through and keep retrying.
            now = time.monotonic()
            if interrupted_since is None:
                # Interruption just began. Surface the warning RIGHT NOW (the Tk side
                # sets the "interrupted" status immediately and schedules the Wait/Cut
                # dialog after HB_WARN_SECS) and reconnect immediately. We must NOT wait
                # for a reconnect attempt to fail first: a real half-open drop (Wi-Fi
                # off, cable pulled, server unreachable) makes the next connect block for
                # its whole handshake timeout, which would otherwise leave the window
                # stale on "connected"/"waiting" with no popup for many seconds. Also
                # mark the link down (so a pending approval that unblocks when we cancel
                # its dialog won't post a stale 'denied') and cancel any pending prompt.
                interrupted_since = now
                self._link_down = True
                notified = True
                self.app.post(self.app._close_approval_dialog)
                self.app.post(self.app.on_link_interrupted)
                continue
            if (not session_lost and self.app._connected
                    and now - interrupted_since > HB_MAX_RECONNECT_SECS):
                # An ACTIVE session couldn't be restored before the viewer's grace
                # elapsed: that viewer is gone (the server has released it). But DON'T
                # give up — drop to idle and keep reconnecting with the SAME code, so a
                # longer internet outage shows the offline placeholder and the agent comes
                # back online (ready for a new viewer, which can resume via its token) as
                # soon as the worker is reachable again, instead of dying on "lost".
                session_lost = True
                self.app.post(self.app.on_session_lost)
            # Already warned on the first drop; just wait a short, stop-interruptible
            # delay before the next reconnect attempt.
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=HB_RECONNECT_SECS)
            except asyncio.TimeoutError:
                pass
        # Loop ended (only a deliberate stop reaches here now): dismiss any dialog.
        self.app.post(self.app.on_link_dismiss)

    async def _heartbeat(self, ws):
        """Ping the server over the WS and watch for the echoed pong. If none comes
        back within HB_INTERRUPT_SECS the link is dead: surface the interruption
        warning RIGHT HERE (at detection) and then close the socket so the reconnect
        loop runs. The warning must be posted BEFORE ws.close(): on a half-open socket
        (Wi-Fi off, cable pulled) close() blocks for close_timeout and the async-with
        teardown that follows is where _run would otherwise post it — so posting only
        from there leaves the window stale on 'connected' for several seconds."""
        self._last_pong = time.time()
        last_tick = time.time()
        wobbling = False  # we've flagged the link as interrupted (yellow) for this gap
        try:
            while True:
                await asyncio.sleep(HB_PING_SECS)
                now = time.time()
                # If far more than the interval elapsed, the process was suspended
                # (machine sleep): the pong is stale only because we weren't running,
                # not because the link failed. Give it a fresh window + re-ping.
                if now - last_tick > HB_INTERRUPT_SECS:
                    self._last_pong = now
                last_tick = now
                # Check the pong gap BEFORE sending. On a real half-open drop (Wi-Fi off,
                # cable pulled; no FIN/RST until the link returns) the ws.send() below can
                # wedge indefinitely on TCP/TLS write backpressure once the write buffer
                # fills, so a gap check placed only AFTER the send would never run and the
                # interruption would surface only when the socket finally broke on
                # recovery (minutes late). Checking first makes detection independent of
                # the send draining.
                gap = time.time() - self._last_pong
                if gap > HB_INTERRUPT_SECS:
                    # Confirmed drop: close + reconnect (and _run schedules the dialog).
                    self._link_down = True
                    self.app.post(self.app.on_link_interrupted)
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                if gap > HB_WOBBLE_SECS:
                    # First missed check(s): surface the interruption IMMEDIATELY (status
                    # goes yellow) so it shows from the first failed check, well before
                    # the Wait/Cut window. If the pong resumes we revert it below.
                    if not wobbling:
                        wobbling = True
                        self.app.post(self.app.on_link_unstable)
                elif wobbling:
                    # The pong came back before a full drop — the link steadied; clear it.
                    wobbling = False
                    self.app.post(self.app.on_link_steady)
                # Send the heartbeat ping, but never block on it forever: on a half-open
                # link the write wedges on backpressure, which is itself a confirmed
                # interruption (and the gap check above has already run for this tick).
                try:
                    await asyncio.wait_for(ws.send("ping"), timeout=HB_INTERRUPT_SECS)
                except asyncio.TimeoutError:
                    self._link_down = True
                    self.app.post(self.app.on_link_interrupted)
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                except Exception:
                    # The send itself failed: the socket is dead. Warn now rather than
                    # leaving the window stale while the teardown runs.
                    self._link_down = True
                    self.app.post(self.app.on_link_interrupted)
                    return
        except asyncio.CancelledError:
            pass

    async def _recv_loop(self, ws):
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            if raw == "pong":
                self._last_pong = time.time()  # heartbeat: our link is alive
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            try:
                await self._handle_msg(ws, msg)
            except Exception:
                # Defense-in-depth: a single malformed message or a bug in one handler
                # must never tear down the live session. Drop it and keep receiving.
                continue

    async def _handle_msg(self, ws, msg):
        """Dispatch one decoded viewer/server message. Called inside _recv_loop's guard."""
        if "t" in msg:
            # Only inject input for an APPROVED, streaming session (ignore anything sent
            # before consent / during the prompt / after a deny), and protect the agent's
            # own windows (see apply_event).
            if not self.streaming:
                return
            apply_event(
                msg, self.monitors,
                getattr(self.app, "_window_rects", None),
                self._blocked_btns, self._pressed_btns,
                getattr(self.app, "_agent_foreground", False),
            )
            return
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
            asyncio.create_task(
                self._on_viewer(
                    ws, bool(msg.get("resumed")), msg.get("vid"), msg.get("vtok")
                )
            )
        elif mtype == "peer-left" and msg.get("who") == "viewer":
            # If a PIN challenge is in progress for this viewer, abort it so the lock is
            # released at once instead of waiting out the challenge deadline.
            c = self._pin_cancel
            if c is not None:
                c.set()
            await self._stop_stream()
            self._viewer_approved = False  # viewer relationship ended; re-consent next
            local = self.ended_locally
            self.ended_locally = False
            self._report_peer_left(local)
        elif mtype == "viewer-interrupted":
            # The server noticed the viewer's link went quiet — its browser tab may
            # be frozen/backgrounded, or its connection briefly dropped. Advisory
            # only: we keep the session and consent intact (the WebRTC media keeps
            # flowing; a frozen tab catches up on unfreeze) and just warn the owner.
            # A genuine departure still arrives as peer-left (relayed or via the
            # media connectionstatechange); a reconnect as viewer-joined.
            self.app.post(self.app.on_viewer_interrupted)
        elif mtype == "viewer-restored":
            # The viewer's link went quiet but it is pinging again: clear the warning.
            self.app.post(self.app.on_viewer_restored)
        elif mtype == "clip":
            # The viewer's clipboard changed; mirror it onto this machine's clipboard.
            # Only for an approved, streaming session (same gate as injected input).
            if not self.streaming:
                return
            text = msg.get("text")
            if isinstance(text, str) and len(text.encode("utf-8")) <= CLIP_MAX_BYTES:
                # Tk is not thread-safe: set the clipboard on the GUI thread, which also
                # records the new clipboard sequence number so the host watcher doesn't
                # echo it straight back to the viewer.
                self.app.post(lambda t=text: self.app.set_remote_clipboard(t))
        elif mtype == "clip-img-begin":
            if self.streaming:
                self._clip_img_rx = {"id": msg.get("id"), "buf": bytearray()}
        elif mtype == "clip-img-chunk":
            rx = self._clip_img_rx
            if (self.streaming and rx and rx["id"] == msg.get("id")
                    and isinstance(msg.get("data"), str)):
                try:
                    raw = base64.b64decode(msg["data"])
                except Exception:
                    raw = b""
                if len(rx["buf"]) + len(raw) <= CLIP_IMG_MAX_BYTES:
                    rx["buf"].extend(raw)
                else:
                    self._clip_img_rx = None  # oversized; drop the transfer
        elif mtype == "clip-img-end":
            rx = self._clip_img_rx
            if self.streaming and rx and rx["id"] == msg.get("id"):
                self._clip_img_rx = None
                png = bytes(rx["buf"])
                if png:
                    self.app.post(lambda p=png: self.app.set_remote_clipboard_image(p))
        elif mtype == "file-begin":
            # Viewer is sending us a file. Same gate as input: approved + streaming.
            if self.streaming:
                await self._on_file_begin(msg)
        elif mtype == "file-chunk":
            if self.streaming:
                await self._on_file_chunk(msg)
        elif mtype == "file-end":
            if self.streaming:
                await self._on_file_end(msg)
        elif mtype == "file-cancel":
            # Either a viewer->host transfer the viewer aborted, or the viewer
            # refusing/aborting a host->viewer transfer we were sending.
            fid = msg.get("id")
            self._cancel_recv(fid, notify=False)
            st = self._send_files.get(fid)
            if st:
                st["cancel"] = True
                st["event"].set()
        elif mtype == "file-ack":
            # Flow control for a host->viewer transfer: the viewer acked chunk n.
            st = self._send_files.get(msg.get("id"))
            if st:
                try:
                    st["acked"] = max(st["acked"], int(msg.get("n", -1)))
                except (TypeError, ValueError):
                    pass
                st["event"].set()
        elif mtype == "pin":
            # A viewer's answer to a PIN challenge (auto-accept gating). Hand it to the
            # waiting _challenge_pin; ignored if no challenge is in progress.
            q = self._pin_queue
            if q is not None:
                q.put_nowait(str(msg.get("pin", "")))

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
        self._viewer_approved = False  # session ended; a new viewer must consent
        self._approved_vtok = None     # ...including the same browser (terminal end)
        await self._stop_stream()
        if ws:
            try:
                await ws.send(json.dumps({"type": "session-ended"}))
            except Exception:
                pass
        self._report_peer_left(False)

    async def _on_viewer(self, ws, resumed=False, vid=None, vtok=None):
        # Serialize viewer admission. Each viewer-joined spawns its own task; without a
        # lock two could interleave their teardown + re-serve and corrupt pc/capture/
        # streaming (and, with auto-accept, start a stream the other then tears down).
        # The generation guard drops a join that a newer one superseded while it waited,
        # so a flapping viewer collapses to a single (latest) admit instead of a backlog
        # of prompts.
        self._viewer_gen += 1
        gen = self._viewer_gen
        # Abort any in-flight PIN challenge so a newer join doesn't wait out the old
        # challenge's deadline behind the viewer lock (the old one bails as superseded).
        c = self._pin_cancel
        if c is not None:
            c.set()
        async with self._viewer_lock:
            if gen != self._viewer_gen:
                return
            await self._serve_viewer(ws, resumed, vid, vtok)

    async def _serve_viewer(self, ws, resumed=False, vid=None, vtok=None):
        if self.streaming:
            # A viewer-joined arrived while we're still streaming to the previous socket.
            # The server only sends one for a genuine new / replacement / held re-serve
            # (a second LIVE viewer is refused busy), so the current stream is stale:
            # tear it down (streaming=False first, so the pc-close doesn't fire
            # _report_peer_left). Then fall through to normal consent below — `resumed`
            # (our OWN reconnect re-serving a still-held viewer, same socket) re-admits
            # silently; a fresh viewer socket (a reconnect/takeover after the old one
            # dropped) is re-prompted, so a different person who has the code cannot
            # silently inherit the previous viewer's approval.
            await self._stop_stream()
        # Is this the SAME approved viewer coming back? Two independent, consent-safe
        # signals say yes:
        #   - resumed + matching vid: the server re-served a still-LIVE held viewer (same
        #     socket) after OUR OWN reconnect — the vid is the exact socket we approved.
        #   - matching vtok: the viewer's own stable resume token (unguessable, held only
        #     by the approved browser) matches the one we approved — so even a viewer that
        #     reconnected on a brand-new socket (its own link dropped, or a full dual-drop)
        #     is provably the same approved browser. A different code-holder carries a
        #     different vtok and is NOT matched. The vtok survives a reconnect; the vid
        #     does not, which is why vtok is what makes "resume without re-consent" work.
        same_approved_vid = (
            resumed and self._viewer_approved and vid is not None and vid == self._approved_vid
        )
        same_approved_vtok = (
            vtok is not None and self._approved_vtok is not None and vtok == self._approved_vtok
        )
        is_resume = same_approved_vid or same_approved_vtok
        # The owner ended this session locally while we were disconnected. A held/returning
        # viewer we'd otherwise resume must be dismissed (told to close), not re-admitted —
        # otherwise the explicit Stop is silently reversed (and, under auto-accept, screen
        # sharing would resume with no consent). A brand-new viewer (no resume match) falls
        # through to earn consent afresh below.
        if is_resume and self._ended:
            self._ended = False
            self._viewer_approved = False
            self._approved_vtok = None
            try:
                await ws.send(json.dumps({"type": "session-ended"}))
            except Exception:
                pass
            return
        # A brand-new viewer relationship (not a resume of the approved viewer) must earn
        # consent afresh — forget any prior approval and clear a stale local-end.
        if not is_resume:
            self._viewer_approved = False
            self._ended = False
        # A fresh viewer: clear any leftover per-session input state.
        self.ended_locally = False
        self._left_reported = False
        self._blocked_btns.clear()
        self._pressed_btns.clear()
        self._last_blockrects = None  # force a fresh blockrects send to this viewer
        # Decide consent. The same approved viewer resuming (by vid on our own reconnect,
        # or by vtok across the viewer's own reconnect — see above) is re-admitted silently;
        # it already earned trust, so no prompt and no PIN. A fresh viewer under auto-accept
        # must present the PIN (if one is set) before it is admitted; everyone else — a
        # first-time viewer with auto-accept off, or a different code-holder who took over
        # the slot — is prompted for manual approval.
        self._pin_superseded = False
        # Snapshot (auto_accept, pin) with a single read of each, and FAIL SAFE: if
        # auto-accept is on but somehow no PIN is set, fall back to the manual prompt
        # rather than silently admitting (never weaken the gate to "code only").
        auto = self.app.auto_accept
        pin = self.app.pin
        if is_resume:
            allowed = True
        elif auto and pin:
            allowed = await self._challenge_pin(ws, pin)
        else:
            allowed = await self.loop.run_in_executor(None, self._ask_approval)
        if not allowed:
            # Make sure no stream survives a denial (defense in depth: the lock already
            # prevents a concurrent task from starting one under us).
            await self._stop_stream()
            # If our link dropped (or this is no longer the current socket) while the
            # owner was deciding, the approval unblocked because the drop force-closed
            # the dialog — not because the owner denied. Likewise a PIN challenge that was
            # cancelled (viewer left / superseded) must not fire a 'denied' at whoever now
            # holds the slot. In those cases the connection is gone / re-served, so don't
            # send 'denied' or flash a spurious 'Denied' status over the reconnection.
            if ws is self.ws and not self._link_down and not self._pin_superseded:
                try:
                    await ws.send(json.dumps({"type": "denied"}))
                except Exception:
                    pass
                self.app.post(self.app.on_denied)
            return
        self._viewer_approved = True  # admitted: re-serve silently across our reconnect
        self._approved_vid = vid      # ...this exact viewer socket (our own reconnect), ...
        if vtok is not None:
            self._approved_vtok = vtok  # ...and this viewer's stable token (its reconnect)
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
        # Advertise optional capabilities so a newer viewer enables its clipboard/file UI
        # only against an agent that supports them (older agents just ignore these types).
        try:
            await ws.send(json.dumps({"type": "caps", "clip": True, "file": True}))
        except Exception:
            pass
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
                    # If OUR link to the server is interrupted (reconnecting), the media
                    # failing is part of that same drop — don't tear the session down as
                    # "viewer left". The reconnect flow re-serves when the link returns;
                    # a genuine viewer departure instead arrives as a relayed peer-left.
                    if self._link_down:
                        return
                    # Media failed but the control link isn't CONFIRMED down yet. On a
                    # whole-network drop (the shared computer loses internet) ICE can reach
                    # "failed" within a second or two — ahead of the heartbeat's ~3s
                    # confirmation. Tearing down here would close the pc, drop _connected,
                    # and report "viewer left" for what is really our own interruption.
                    # So wait briefly: if the heartbeat flags the control link down, defer
                    # and let the reconnect flow drive the warn + re-serve. Only a lasting
                    # media failure with a still-healthy control link is a real "viewer
                    # gone" (also normally signalled faster by a relayed peer-left).
                    for _ in range(int(HB_INTERRUPT_SECS / 0.25) + 2):
                        await asyncio.sleep(0.25)
                        if self._link_down or not self.streaming or self.pc is not pc:
                            return
                    if self._link_down:
                        return
                    # The media path died on its own; fall back to waiting for a viewer.
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

    async def _challenge_pin(self, ws, pin):
        """Auto-accept is on with a PIN set: admit only a viewer that presents it.

        Returns True to admit, False to deny. We send `pin-required`, then read the
        viewer's `pin` answers (delivered via _pin_queue from _handle_msg) and compare in
        constant time. Hardening:
          - brute force is bounded by a CUMULATIVE wrong-answer count that survives
            reconnects; once it reaches PIN_LOCKOUT the agent refuses every challenge for a
            cooldown that DOUBLES on each repeat lockout (up to PIN_COOLDOWN_MAX_S), and the
            owner is alerted — so a code-holder can't grind the PIN at a fixed rate.
          - the WHOLE challenge has one wall-clock deadline (PIN_TIMEOUT_S), so a drip of
            wrong answers can't hold the viewer-admission lock open indefinitely.
          - the wait is cancellable: if the viewer leaves (peer-left) or a newer viewer
            supersedes it, _pin_cancel is set and we bail without a spurious 'denied'.
          - a non-ASCII / malformed answer counts as a wrong attempt (never crashes the
            compare). The PIN itself is never logged or echoed."""
        now = self.loop.time()
        if self._pin_fails >= PIN_LOCKOUT:
            if now < self._pin_lock_until:
                return False          # still cooling down from too many wrong PINs
            self._pin_fails = 0       # cooldown elapsed — allow guesses again (backoff kept)
        self._pin_queue = asyncio.Queue()
        self._pin_cancel = asyncio.Event()
        self._pin_superseded = False
        deadline = self.loop.time() + PIN_TIMEOUT_S
        try:
            try:
                await ws.send(json.dumps({"type": "pin-required", "len": len(pin)}))
            except Exception:
                return False
            while True:
                remaining = deadline - self.loop.time()
                if remaining <= 0:
                    return False      # whole-challenge deadline hit
                getter = asyncio.ensure_future(self._pin_queue.get())
                canceller = asyncio.ensure_future(self._pin_cancel.wait())
                try:
                    done, _ = await asyncio.wait(
                        {getter, canceller}, timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (getter, canceller):
                        if not task.done():
                            task.cancel()
                if canceller in done:
                    self._pin_superseded = True
                    return False      # viewer left / superseded: bail without 'denied'
                if getter not in done:
                    return False      # timed out waiting for an answer
                sub = getter.result()
                if ws is not self.ws or self._link_down:
                    return False      # our link dropped while we waited
                ok = False
                if isinstance(sub, str) and sub.isascii():
                    try:
                        ok = secrets.compare_digest(sub, str(pin))
                    except Exception:
                        ok = False    # defensive: never let a bad value crash the compare
                if ok:
                    self._pin_fails = 0
                    self._pin_backoff = 0
                    return True
                self._pin_fails += 1
                if self._pin_fails >= PIN_LOCKOUT:
                    cooldown = min(PIN_COOLDOWN_S * (2 ** self._pin_backoff),
                                   PIN_COOLDOWN_MAX_S)
                    self._pin_backoff += 1
                    self._pin_lock_until = self.loop.time() + cooldown
                    self.app.post(self.app.on_pin_blocked)  # alert the owner
                    return False      # too many wrong PINs: lock out and deny
                left = PIN_LOCKOUT - self._pin_fails
                try:
                    await ws.send(json.dumps({"type": "pin-bad", "left": left}))
                except Exception:
                    return False
        finally:
            self._pin_queue = None
            self._pin_cancel = None

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
        self._reset_transfers()  # discard any in-flight file transfers for this session
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
            # Drop the peer connection's UDP sockets BEFORE closing it. On a whole-network
            # drop (the shared computer loses internet) aiortc's pc.close() blocks for a
            # long time waiting on sockets that are open but unreachable — which freezes
            # this net thread's event loop, so the heartbeat never detects the drop (no
            # warning) and the reconnect never runs. Closing the sockets first makes
            # close() return promptly; wait_for bounds it as a backstop either way.
            _force_close_ice_sockets(pc)
            try:
                await asyncio.wait_for(pc.close(), timeout=HB_INTERRUPT_SECS)
            except Exception:
                pass
        if self.capture:
            cap = self.capture
            self.capture = None
            try:
                await asyncio.wait_for(
                    self.loop.run_in_executor(None, cap.stop), timeout=HB_INTERRUPT_SECS)
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
        # Hidden until positioned: setting a position-only geometry before the window is
        # mapped doesn't stick (it maps at 0,0), so build + size it hidden, then center it
        # over the parent with a full geometry and only then show it (also avoids a flash).
        self.withdraw()
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
        self.deiconify()  # now mapped at the centered geometry
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
        """Center over the parent window, clamped to stay on screen. Sets a FULL geometry
        (size + position) while hidden so it sticks when the window is shown."""
        try:
            self.update_idletasks()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            x = px + (pw - w) // 2
            y = py + (ph - h) // 2
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            x = max(0, min(x, sw - w))  # keep fully on screen
            y = max(0, min(y, sh - h))
            self.geometry("%dx%d+%d+%d" % (w, h, x, y))
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

    @staticmethod
    def _two_lines(text):
        """Split one line into two balanced lines at the space nearest the middle,
        keeping any leading bullet on the first line. Unchanged if it has no space."""
        s = text.strip()
        spaces = [i for i, c in enumerate(s) if c == " " and i > 0]
        if not spaces:
            return s
        mid = len(s) / 2
        best = min(spaces, key=lambda i: abs(i - mid))
        return s[:best] + "\n" + s[best + 1:]

    def set_text(self, text):
        self._text = self._two_lines(text)
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
                         font=self._font, justify="center")


class App:
    def __init__(self, args):
        self.args = args
        self.auto_accept = bool(args.auto_accept)
        # PIN that gates auto-accept (generated whenever auto-accept is on; None when off).
        # A fresh viewer must present it to be admitted silently (see NetClient._challenge_pin).
        self.pin = _new_pin() if self.auto_accept else None
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
        self._interrupt_dialog = None  # the "connection interrupted" Wait/Cut dialog
        self._interrupt_grace = None   # after() id: delay before the dialog first shows
        self._interrupt_reask = None   # after() id for re-asking 10s after "Wait"
        self._window_rects = ()      # our windows' screen rects (main + any dialog);
        #                              injected clicks on them are ignored, but only where
        #                              one of our windows is actually the top-most window
        #                              (see _click_on_agent — a click on another window that
        #                              covers us is allowed through).
        self._agent_foreground = False  # an agent window has OS focus -> drop injected keys
        self._clip_seq = None         # last OS clipboard sequence number we reconciled;
        #                               a change means the local clipboard changed, and we
        #                               set it to our own writes so they don't echo back.
        self._clip_after = None       # after() id for the clipboard poll loop

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

        # Auto-accept PIN, shown under the checkbox while auto-accept is on so the owner
        # can share it. Both lines keep a fixed height so toggling the PIN on/off never
        # changes the (fixed-size) window: height=1 each reserves the line even when blank.
        self.pin_caption = tk.Label(
            self.root, text="", bg=BG, fg=MUTED,
            font=("Segoe UI", 8, "bold"), height=1,
        )
        self.pin_caption.pack()
        self.pin_label = tk.Label(
            self.root, text="", bg=BG, fg=TEXT,
            font=("Consolas", 16, "bold"), height=1,
        )
        self.pin_label.pack(pady=(0, 6))
        self._refresh_pin_label()

        # Theme selector (Auto / Light / Dark), applied live.
        self._build_theme_selector()

        # The approval prompt is shown as its own window (ActionDialog), created on
        # demand in show_approval(); it is no longer an in-window overlay.

        # Connected banner (hidden until streaming) — rounded bottom, fades in/out.
        self.banner = RoundedBanner(
            self.root, parent_bg=BG, fill=BANNER_BG, fg=BANNER_FG,
            font=tkfont.Font(family="Segoe UI Semibold", size=10),
        )

        # Session controls (Send file + Stop) share one row so they reserve a single
        # row's height in the fixed window; shown only while a viewer is connected.
        self.action_row = tk.Frame(self.root, bg=BG)
        self.send_btn = RoundedButton(
            self.action_row, tr("agent.btn.sendFile"), NEUTRAL_BG, NEUTRAL_FG,
            command=self.send_file_click,
            font=("Segoe UI Semibold", 10), min_width=0, pad_x=14, parent_bg=BG,
            border=BORDER,
        )
        self.send_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = RoundedButton(
            self.action_row, tr("agent.btn.stop"), STOP_BG, STOP_FG,
            command=self.end_session_click,
            font=("Segoe UI Semibold", 10), min_width=150, parent_bg=BG,
        )
        self.stop_btn.pack(side="left")
        # Packed now so the base-layout measurement below RESERVES room for the row in
        # the fixed window height; it is pack_forget()'d again before the window is
        # shown (idle state has no controls) and re-packed in on_connected.
        self.action_row.pack(pady=(6, 14))

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

        # Base layout is measured; hide the controls row so it only appears while a
        # viewer is connected (its height stays reserved in the fixed window).
        self.action_row.pack_forget()

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
            self.action_row.pack(pady=(6, 14))
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
        prev_ctx = None
        try:
            import ctypes
            from ctypes import wintypes

            u = ctypes.windll.user32
            u.GetWindow.restype = wintypes.HWND
            u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
            u.IsWindowVisible.argtypes = [wintypes.HWND]
            u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            u.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            # Query rects in PHYSICAL pixels so they line up with the mss capture / stream
            # and the per-monitor-aware hit-test (and the injected click coords). Our window
            # is deliberately DPI-UNAWARE (making it DPI-aware shifts where it lands in the
            # BitBlt capture), so by default GetWindowRect on this Tk thread would return
            # LOGICAL pixels and the viewer's blocked-cursor rects would be offset on a
            # scaled display. Switch just this thread to per-monitor-v2 for the calls below.
            try:
                u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
                u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                prev_ctx = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                prev_ctx = None

            # Find OUR OWN visible top-level windows straight from the OS, by owning process
            # (main window + any open dialog). This uses the DECORATED windows the OS
            # actually stacks and hit-tests — unlike GetAncestor(winfo_id, GA_ROOT), which can
            # return a hidden Tk wrapper window at a different Z-order position, which made the
            # covering-window walk below miss some apps (so a covered area was wrongly flagged
            # as blocked and showed the forbidden cursor). Identify ours by pid, as the click
            # filter does. Hidden helper windows (ttk monitor, IME) are skipped by size/visibility.
            our_pid = os.getpid()
            our_windows = []
            def _enum_cb(h, lp):
                try:
                    if not u.IsWindowVisible(h):
                        return True
                    pid = wintypes.DWORD()
                    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
                    if pid.value != our_pid:
                        return True
                    r = wintypes.RECT()
                    if not u.GetWindowRect(h, ctypes.byref(r)):
                        return True
                    if (r.right - r.left) < 8 or (r.bottom - r.top) < 8:
                        return True  # skip tiny/hidden helper windows
                    our_windows.append((int(h), (r.left, r.top, r.right, r.bottom)))
                except Exception:
                    pass
                return True
            _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            u.EnumWindows(_EnumProc(_enum_cb), 0)

            u.WindowFromPoint.argtypes = [wintypes.POINT]  # POINT passed BY VALUE
            u.WindowFromPoint.restype = ctypes.c_void_p    # HWND is 64-bit; don't truncate

            def _sampled_visible(full):
                # The sub-rectangles of `full` where one of OUR windows is actually the TOP
                # window, found by sampling WindowFromPoint on a grid — the SAME test the
                # click filter (_click_on_agent) uses. This guarantees the viewer's blocked-
                # cursor region matches exactly where clicks are refused, and is robust to
                # DPI and Tk's wrapper windows (a Z-order walk from a cached handle gave the
                # wrong covering set). Cells are STEP px; adjacent rows with the same spans
                # are merged into taller rects to keep the list small.
                fl, ft, fr, fb = full
                STEP = 16

                def _row_spans(y):
                    spans = []
                    start = None
                    x = fl
                    while x < fr:
                        on = False
                        try:
                            h = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
                            if h:
                                pid = wintypes.DWORD()
                                u.GetWindowThreadProcessId(h, ctypes.byref(pid))
                                on = (pid.value == our_pid)
                        except Exception:
                            on = False
                        if on and start is None:
                            start = x
                        elif not on and start is not None:
                            spans.append((start, x))
                            start = None
                        x += STEP
                    if start is not None:
                        spans.append((start, fr))
                    return spans

                out = []
                prev = None
                top = ft
                y = ft
                while y < fb:
                    s = _row_spans(y)
                    if s != prev:
                        if prev:
                            for (x0, x1) in prev:
                                out.append((x0, top, x1, y))
                        prev = s
                        top = y
                    y += STEP
                if prev:
                    for (x0, x1) in prev:
                        out.append((x0, top, x1, fb))
                return out

            for _hwnd, full in our_windows:
                rects.extend(_sampled_visible(full))

            global _GUI_DPI_LOGGED
            if _CLICK_DEBUG and not _GUI_DPI_LOGGED:
                _GUI_DPI_LOGGED = True
                _click_log(f"[diag gui-thread DPI={_dpi_awareness_name()} "
                           f"our_windows={our_windows} tk_scaling="
                           f"{self.root.tk.call('tk', 'scaling')}]")

            # Are WE the foreground app? Compare by owning process (handle identity is
            # unreliable with Tk's wrapper windows — see _click_on_agent), so injected
            # keys are dropped whenever any of our windows has focus.
            self._agent_foreground = (
                _window_owner_pid(u.GetForegroundWindow()) == our_pid)
        except Exception:
            pass
        finally:
            if prev_ctx:
                try:
                    ctypes.windll.user32.SetThreadDpiAwarenessContext(prev_ctx)
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
                self.action_row.pack_forget()

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
        # Show the session controls (Send file + Stop) only while a viewer is connected
        # (packed back into the normal flow, below the checkbox). Room for the row is
        # reserved in the fixed window height. The Stop button fades in from the bg.
        self.action_row.pack(pady=(6, 14))
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
        # A denial can now land AFTER a session was established (a reconnecting viewer is
        # re-prompted), so tear the controlling UI down like on_peer_left — otherwise the
        # 'controlling' banner + Stop button linger over a 'Denied' status.
        self._fade_banner(False)
        self._fade_stop(False)
        self.set_status(tr("agent.status.denied"), WARN)

    def on_pin_blocked(self):
        # Someone entered too many wrong PINs and tripped the lockout — make the owner
        # aware (an attack, or a viewer who was given the wrong/old PIN). Don't disturb a
        # live controlling session's status.
        if not self._connected:
            self.set_status(tr("agent.status.pinBlocked"), WARN)

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

    def on_viewer_interrupted(self):
        """Advisory: the viewer's link went quiet (its tab may be frozen/backgrounded,
        or its connection briefly dropped). We keep streaming + consent; just warn the
        owner. No-op if we aren't in a controlling session (e.g. the viewer already
        left and a stale advisory arrived)."""
        if not self._connected:
            return
        self.set_status(tr("agent.status.viewerInterrupted"), WARN)
        self.banner.set_text(tr("agent.banner.viewerInterrupted"))

    def on_viewer_restored(self):
        """The viewer is reachable again: restore the controlling status/banner."""
        if not self._connected:
            return
        self.set_status(tr("agent.status.connected"), GOOD)
        self.banner.set_text(tr("agent.banner.controlling"))

    def on_fatal(self, message):
        self.set_status(tr("agent.error.couldNotStart"), BAD)
        messagebox.showerror("Audiobus Remote", message)

    # ---- Connection-interruption dialog (same prompt as the webapp) ----

    def on_link_unstable(self):
        """First missed heartbeat(s): the link looks interrupted. Show it IMMEDIATELY —
        turn the green 'connected' label yellow with the interruption text (or, when idle,
        the offline placeholder) — before the full-drop handling and the Wait/Cut window.
        on_link_steady reverts it if the pong resumes without a full drop."""
        if self._connected:
            self.set_status(tr("ui.interrupt.status"), WARN)
        else:
            self.code_panel.set_text(CODE_PLACEHOLDER)
            self.set_status(tr("agent.status.offline"), WARN)

    def on_link_steady(self):
        """The heartbeat recovered before a full interruption: restore the normal state
        (the early-warning yellow was a brief wobble, not a real drop)."""
        if self._connected:
            self.set_status(tr("agent.status.connected"), GOOD)
        else:
            if self._code:
                self.code_panel.set_text(self._code)
            self.set_status(tr("agent.status.waiting"), WARN)

    def on_link_interrupted(self):
        """Our established link to the server dropped. The net thread is already
        reconnecting; show the Wait/Cut warning only if we're still down after a
        short grace, so a quick reconnect stays invisible."""
        if not self._connected:
            # No active session to preserve — the worker link just went quiet (e.g. the
            # internet dropped). There is nothing to "keep waiting" for, so DON'T show
            # the Wait/Cut dialog. Instead show a placeholder code (this computer isn't
            # reachable, so its code can't be used) + a quiet reconnecting status. The
            # real code is restored on reconnect (on_reconnected).
            self.code_panel.set_text(CODE_PLACEHOLDER)
            self.set_status(tr("agent.status.offline"), WARN)
            return
        # Yellow (WARN), not red: this is a recoverable interruption, not a failure.
        self.set_status(tr("ui.interrupt.status"), WARN)
        if self._interrupt_dialog is not None or self._interrupt_grace is not None:
            return  # dialog already showing or already scheduled
        self._interrupt_grace = self.root.after(
            int(HB_WARN_SECS * 1000), self._interrupt_grace_fire)

    def _interrupt_grace_fire(self):
        self._interrupt_grace = None
        self._show_interrupt_dialog()

    def _show_interrupt_dialog(self):
        dlg = self._interrupt_dialog
        if dlg is not None:
            try:
                if dlg.winfo_exists() and not dlg._done:
                    return  # already showing
            except Exception:
                pass
        self._cancel_interrupt_reask()
        self._interrupt_dialog = ActionDialog(
            self.root, tr("ui.interrupt.msg"),
            tr("ui.interrupt.cut"), STOP_BG, STOP_FG,
            tr("ui.interrupt.wait"), NEUTRAL_BG, NEUTRAL_FG,
            on_pos=self._interrupt_cut, on_neg=self._interrupt_wait,
        )

    def _interrupt_cut(self):
        # Cut: the owner chose to shut this session down during the interruption (the
        # link is down, so we can't tell the viewer right now). END the session rather
        # than quitting the agent: end_session() records it (_ended), so the moment we
        # reconnect, the held viewer is re-served and told "session-ended" (connect page
        # -> disconnected). The agent stays alive and keeps reconnecting; it is now idle
        # + offline, so it shows the placeholder, and recovers to "waiting" (same code)
        # once the worker is reachable again.
        self._interrupt_dialog = None
        self._cancel_interrupt_timers()
        self._connected = False
        if self.client:
            self.client.end_session()
        self._fade_banner(False)
        self._fade_stop(False)
        self.code_panel.set_text(CODE_PLACEHOLDER)
        self.set_status(tr("agent.status.offline"), WARN)

    def _interrupt_wait(self):
        # Wait: keep reconnecting; re-ask after 10s if still down (on_reconnected
        # cancels this).
        self._interrupt_dialog = None
        self._interrupt_reask = self.root.after(
            INTERRUPT_REASK_MS, self._interrupt_reask_fire)

    def _interrupt_reask_fire(self):
        self._interrupt_reask = None
        self._show_interrupt_dialog()

    def _cancel_interrupt_reask(self):
        if self._interrupt_reask is not None:
            try:
                self.root.after_cancel(self._interrupt_reask)
            except Exception:
                pass
            self._interrupt_reask = None

    def _cancel_interrupt_timers(self):
        self._cancel_interrupt_reask()
        if self._interrupt_grace is not None:
            try:
                self.root.after_cancel(self._interrupt_grace)
            except Exception:
                pass
            self._interrupt_grace = None

    def on_link_dismiss(self):
        """Close the interruption dialog and cancel its timers (teardown/recovery)."""
        self._cancel_interrupt_timers()
        dlg = self._interrupt_dialog
        if dlg is not None:
            self._interrupt_dialog = None
            try:
                if dlg.winfo_exists() and not dlg._done:
                    dlg._finish(None)  # close WITHOUT firing the Wait callback
            except Exception:
                pass

    def on_reconnected(self):
        """The link came back: dismiss the dialog/timers and reset to the WAITING state.
        We must NOT restore 'connected' from the pre-drop flag: the viewer may have left
        while we were away, and the server can't always tell us (it can't reliably
        distinguish a half-open agent socket from a live one). If a viewer is still held,
        the server re-serves it (viewer-joined resumed -> _on_viewer -> on_connected),
        which restores the 'controlling' banner; otherwise we correctly fall back to
        'waiting' instead of sitting on a stale 'Connected' with no viewer."""
        self.on_link_dismiss()
        self._connected = False
        self._fade_banner(False)
        self._fade_stop(False)
        # Restore the real code if a worker-link loss had replaced it with the
        # offline placeholder.
        if self._code:
            self.code_panel.set_text(self._code)
        self.set_status(tr("agent.status.waiting"), WARN)

    def on_link_failed(self):
        """Reconnection gave up (the server's grace elapsed without recovery): close
        the dialog and show a terminal 'connection lost' status."""
        self.on_link_dismiss()
        self._connected = False
        self._fade_banner(False)
        self._fade_stop(False)
        self.set_status(tr("ui.interrupt.failed"), BAD)

    def on_session_lost(self):
        """The viewer's reconnection grace elapsed during a long outage — that viewer is
        gone — but the outage itself may still recover. Unlike on_link_failed this is NOT
        terminal: the net thread keeps reconnecting (now as idle), so we drop the
        'controlling' UI and the Wait/Cut dialog and show the offline placeholder +
        'reconnecting' status. When the link returns the real code is restored
        (on_reconnected) and the agent is ready for a new viewer to join and resume."""
        self.on_link_dismiss()
        self._connected = False
        self._fade_banner(False)
        self._fade_stop(False)
        self.code_panel.set_text(CODE_PLACEHOLDER)
        self.set_status(tr("agent.status.offline"), WARN)

    def copy_code(self):
        code = self.code_panel.get_text()
        if code and "·" not in code:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.copy_btn.set_text(tr("agent.btn.copied"))
            self.root.after(1500, lambda: self.copy_btn.set_text(tr("agent.btn.copy")))

    def set_remote_clipboard(self, text):
        """Apply clipboard TEXT received from the viewer onto this machine's clipboard
        (GUI thread), then record the new clipboard sequence number so the poll loop sees
        no change and doesn't bounce it back to the viewer."""
        try:
            self.root.clipboard_clear()
            if text:
                self.root.clipboard_append(text)
        except Exception:
            pass
        self._clip_seq = _clipboard_seq()

    def set_remote_clipboard_image(self, png_bytes):
        """Apply a clipboard IMAGE received from the viewer (GUI thread) via CF_DIB, then
        record the new sequence number so our own write doesn't echo back."""
        try:
            img = Image.open(io.BytesIO(png_bytes))
            img.load()
            _set_clipboard_image(img)
        except Exception:
            pass
        self._clip_seq = _clipboard_seq()

    def _clip_tick(self):
        """Watch the OS clipboard on the GUI thread and push local changes to the viewer.
        We gate on the clipboard sequence number (cheap) and only inspect the contents when
        it changes — which also avoids re-grabbing a large image every tick. On (re)connect
        we take the current sequence as the baseline WITHOUT sending, so nothing pre-existing
        is leaked; our own writes bump _clip_seq in set_remote_clipboard*, echo-guarding the
        loop for both text and images."""
        try:
            if not self._connected or not self.client:
                self._clip_seq = None  # drop the baseline; re-sync on the next session
            else:
                seq = _clipboard_seq()
                if seq != self._clip_seq:
                    first = self._clip_seq is None
                    self._clip_seq = seq
                    if not first:  # first poll of a session is just the baseline
                        self._reconcile_clipboard()
        except Exception:
            pass
        self._clip_after = self.root.after(CLIP_POLL_MS, self._clip_tick)

    def _reconcile_clipboard(self):
        """The clipboard changed locally — send its text, or else its image, to the viewer."""
        text = None
        try:
            text = self.root.clipboard_get()  # TclError if empty or non-text (e.g. an image)
        except Exception:
            text = None
        if isinstance(text, str) and text != "":
            if len(text.encode("utf-8")) <= CLIP_MAX_BYTES:
                self.client.send_clip(text)
            return
        try:
            img = ImageGrab.grabclipboard()  # PIL image, a list of file paths, or None
        except Exception:
            img = None
        if isinstance(img, Image.Image):
            try:
                png = _encode_png(img)
            except Exception:
                png = None
            if png and len(png) <= CLIP_IMG_MAX_BYTES:
                self.client.send_clip_image(png)

    def send_file_click(self):
        """Host->viewer: pick a local file and stream it to the connected viewer."""
        if not self._connected or not self.client:
            return
        try:
            path = filedialog.askopenfilename(
                parent=self.root, title=tr("agent.file.pickTitle"))
        except Exception:
            path = None
        if path:
            self.client.send_file(path)

    def on_file_progress(self, name, got, size, outgoing):
        """Transfer progress (either direction). Shown in the status line, throttled by
        the caller's chunk cadence; harmless if a stray late update arrives after a
        session ends (guarded by _connected)."""
        if not self._connected:
            return
        pct = int(got * 100 / size) if size else 100
        key = "agent.file.sending" if outgoing else "agent.file.receiving"
        try:
            self.set_status(tr(key).format(name=name, pct=pct), BRAND)
        except Exception:
            self.set_status(f"{name} — {pct}%", BRAND)

    def on_file_done(self, name, path, outgoing, ok):
        if ok:
            key = "agent.file.sent" if outgoing else "agent.file.received"
            color = GOOD
        else:
            key = "agent.file.failed"
            color = BAD
        try:
            self.set_status(tr(key).format(name=name), color)
        except Exception:
            self.set_status(name, color)
        self.root.after(2500, self._restore_status_after_transfer)

    def _restore_status_after_transfer(self):
        # Return to the normal connected status once no transfer is in flight.
        c = self.client
        if self._connected and c and not (c._recv_files or c._send_files):
            self.set_status(tr("agent.status.connected"), GOOD)

    def _apply_dpi_scaling(self):
        """Match Tk's point->pixel scaling to the display DPI. The process is now DPI-aware
        (see _set_dpi_awareness), so without this, point-sized fonts would render tiny on a
        scaled display (the OS no longer bitmap-stretches the window). At 100% this equals
        Tk's default, so it is a no-op there."""
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()  # Windows 10 1607+
            if dpi and dpi > 0:
                self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

    def _refresh_pin_label(self):
        """Show the PIN under the auto-accept box when auto-accept is on, else blank."""
        on = bool(self.auto_accept and self.pin)
        self.pin_caption.config(text=tr("agent.pin.caption") if on else "")
        self.pin_label.config(text=self.pin if on else "")

    def _toggle_auto(self):
        # Order the two writes so the net thread (which reads auto_accept then pin in
        # _serve_viewer) can never observe the unsafe transient auto_accept=True / pin=None:
        # when ENABLING, set the PIN first; when DISABLING, clear auto_accept first.
        on = self.auto_var.get()
        if on:
            self.pin = _new_pin()      # mint a fresh PIN before enabling
            self.auto_accept = True
        else:
            self.auto_accept = False   # disable before dropping the PIN
            self.pin = None
        # A fresh enable/disable forgives any accumulated wrong-attempt lockout + backoff.
        if self.client is not None:
            self.client._pin_fails = 0
            self.client._pin_backoff = 0
            self.client._pin_lock_until = 0.0
        self._refresh_pin_label()

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
        for aid in list(self._anims.values()) + [self._drain_after, self._clip_after]:
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
        self._wd_tick = time.time()
        self._wd_grace_until = 0.0
        self.root.after(1000, self._link_watchdog)
        self._clip_after = self.root.after(CLIP_POLL_MS, self._clip_tick)
        self.root.mainloop()

    def _link_watchdog(self):
        """Backup drop-detector on the GUI thread. The net thread normally detects a
        dropped link within ~HB_INTERRUPT_SECS, but a hung WebRTC teardown could stall
        its event loop so the warning never appears and it can't reconnect. This GUI
        thread stays responsive, so here we watch the same pong clock independently: if
        the link looks dropped (stale pong) during an active session and the net thread
        hasn't flagged it, surface the interruption and mark the link down so its
        teardown/reconnect treats it as a drop and recovers once its loop frees up."""
        now = time.time()
        # If we were ourselves frozen (device sleep), don't judge for a moment: the pong
        # is stale only because nothing was running, and the net thread needs a round-trip
        # to refresh it.
        if now - self._wd_tick > HB_INTERRUPT_SECS:
            self._wd_grace_until = now + HB_INTERRUPT_SECS
        self._wd_tick = now
        c = getattr(self, "client", None)
        if (c is not None and self._connected and now >= self._wd_grace_until
                and not getattr(c, "_link_down", False)
                and (now - getattr(c, "_last_pong", now)) > HB_INTERRUPT_SECS):
            c._link_down = True
            self.on_link_interrupted()
        self._wd_after = self.root.after(1000, self._link_watchdog)


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
    # Hidden diagnostic: validate this (possibly frozen) build's capture + VP8
    # encode/decode stack headlessly, writing the result to the given file.
    p.add_argument("--selfcheck", default=None, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def run_selfcheck(out_path):
    """Headless check that this build can grab the screen and run the full
    capture -> VP8 encode -> decode path over a loopback peer connection (no GUI).
    Writes 'OK ...' or 'FAIL ...' to out_path and returns it. Used to validate the
    frozen .exe's bundled native libraries (aiortc / PyAV / pylibsrtp)."""

    class _Args:
        fps = 10
        scale = 0.5
        max_width = 1920
        bitrate = None

    class _Client:
        def __init__(self):
            with ScreenGrabber() as sct:
                mons = [dict(m) for m in sct.monitors]
            phys = mons[1:] if len(mons) > 1 else mons[:1]
            m = phys[0]
            self.monitors = [{"i": 0, "left": m["left"], "top": m["top"],
                              "width": m["width"], "height": m["height"]}]
            self.wanted = [0]
            self.args = _Args()
            self.capture = None

        def scale_for(self, w):
            if self.args.scale:
                return self.args.scale
            return min(1.0, self.args.max_width / w) if w > 0 else 1.0

    async def _run():
        c = _Client()
        c.capture = CaptureManager(c)
        c.capture.start()
        sender = RTCPeerConnection()
        receiver = RTCPeerConnection()
        frames = []

        @receiver.on("track")
        def _on_track(track):
            async def _read():
                try:
                    for _ in range(3):
                        f = await track.recv()
                        frames.append((f.width, f.height))
                except Exception:
                    pass
            asyncio.ensure_future(_read())

        sender.addTrack(ScreenTrack(c, 0))
        await sender.setLocalDescription(await sender.createOffer())
        await receiver.setRemoteDescription(sender.localDescription)
        await receiver.setLocalDescription(await receiver.createAnswer())
        await sender.setRemoteDescription(receiver.localDescription)
        for _ in range(120):
            await asyncio.sleep(0.1)
            if len(frames) >= 2:
                break
        await sender.close()
        await receiver.close()
        c.capture.stop()
        return frames

    try:
        frames = asyncio.run(_run())
        result = ("OK frames=%s" % (frames[:2],)) if len(frames) >= 2 \
            else "FAIL no frames decoded"
    except Exception as e:
        result = "FAIL %r" % (e,)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result)
    except Exception:
        pass
    return result


def _set_dpi_awareness():
    """Make the whole process per-monitor DPI-aware BEFORE any Tk window or ScreenGrabber
    exists, so every thread shares ONE coordinate space.

    Without this, Tk windows are created while the process is still DPI-unaware and mss
    later flips the process to per-monitor aware on the NETWORK thread (see the note at
    ScreenGrabber() in NetClient._run). On a SCALED display (e.g. 125%) that leaves the GUI
    thread's window rects in LOGICAL pixels while the click hit-test (WindowFromPoint) and
    pynput injection run in PHYSICAL pixels — so the blocked region and the actual block
    land in different places and the filter wrongly blocks clicks that reach a foreground
    app. At 100% the two spaces coincide, which is why it only shows up when scaled.

    Best-effort across Windows versions; a harmless no-op where the APIs are missing."""
    how = "none"
    try:
        # Per-Monitor-Aware v2 (Windows 10 1703+): DPI_AWARENESS_CONTEXT = (HANDLE)-4.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            how = "ctx-v2"
        else:
            raise OSError(ctypes.get_last_error())
    except Exception as e1:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            how = "shcore-2"
        except Exception as e2:
            try:
                ctypes.windll.user32.SetProcessDPIAware()  # system-aware fallback (Vista+)
                how = "user32-system"
            except Exception:
                how = f"FAILED ({e1!r})"
    if _CLICK_DEBUG:
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except Exception:
            dpi = "?"
        _click_log(f"[diag build={_AGENT_BUILD} set_dpi_awareness={how} "
                   f"thread_dpi_ctx={_dpi_awareness_name()} GetDpiForSystem={dpi}]")


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if getattr(args, "selfcheck", None):
        print(run_selfcheck(args.selfcheck))
        return
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
