# Audiobus Remote

Consent-based remote control. Run a small agent on the computer you want to reach;
it shows a one-time **10-character session code**. Enter that code in the web app
and you can see and control that computer from your browser.

Same model as TeamViewer / AnyDesk / RustDesk: the machine being controlled is in
charge — it runs the agent in the foreground, approves each connection, shows a
banner while connected, and ends the session the instant the agent is closed.

## Architecture

```
  Controlled computer                Cloudflare                     Browser
  ┌────────────────┐        ┌──────────────────────────┐      ┌──────────────┐
  │  audiobus-agent   │  WSS   │  Worker  →  Durable Object │  WSS │  /connect    │
  │  (Python .exe) │ ─────► │  (one per session code)    │ ◄─── │  page        │
  │  screen capture│  JPEG  │  relays frames  ⇄  input   │ input│  <canvas>    │
  │  input inject  │ frames └──────────────────────────┘      └──────────────┘
  └────────────────┘
```

- **Worker** (`src/index.ts`) — serves the web app, mints session codes, and
  upgrades the agent/viewer WebSockets.
- **Durable Object** (`src/session.ts`) — one instance per code (via
  `getByName(code)`). It is the meeting point for the two sides and relays
  messages using the WebSocket Hibernation API. Screen frames (binary) go
  agent → viewer; input events (JSON) go viewer → agent.
- **Web app** (`public/`) — a landing page, a `/share` page, and the `/connect`
  controller that renders frames to a `<canvas>` and forwards mouse/keyboard.
- **Agent** (`agent/audiobus_agent.py`) — captures the screen with `mss`, encodes
  JPEG with Pillow, and applies input with `pynput`.

**Session code:** 10 characters from `23456789ABCDEFGHJKMNPQRSTUVWXYZ`
(alphanumeric, minus the look-alikes `0/O`, `1/I/L`), generated with a CSPRNG.

## Develop

```bash
npm install
npm run dev        # wrangler dev — local server with a simulated Durable Object
```

Open the printed localhost URL. To exercise the agent against local dev:

```bash
cd agent
python -m pip install -r requirements.txt
python audiobus_agent.py --server http://127.0.0.1:8787
```

## Deploy

```bash
npm run deploy     # wrangler deploy
```

This publishes to `https://audiobus-remote.<your-subdomain>.workers.dev`. After the
first deploy, set that URL as the agent's default (edit `DEFAULT_SERVER` in
`agent/audiobus_agent.py`) or pass `--server` when running it.

## Build the downloadable agent (Windows)

Use a **python.org** or Microsoft Store Python 3.10–3.12 (not the MSYS2 mingw
build) so PyInstaller produces a working native `.exe`:

```powershell
cd agent
./build.ps1
```

This creates `agent/dist/audiobus-agent.exe` and copies it to
`public/downloads/audiobus-agent.exe`, which the `/share` page offers as a download.
Run `wrangler deploy` again to publish it. The `.exe` is unsigned, so Windows
SmartScreen may warn on first run until it is code-signed.

## Configuration (agent)

| Flag | Default | Meaning |
|------|---------|---------|
| `--server` | `$AUDIOBUS_SERVER` or built-in | Audiobus server URL |
| `--fps` | `12` | Target frames per second |
| `--quality` | `55` | JPEG quality (1–95) |
| `--max-width` | `1600` | Auto-downscale frames to at most this width |
| `--scale` | auto | Fixed downscale factor instead of `--max-width` |
| `--monitor` | `1` | Monitor to share (1 = primary, 0 = all) |
| `--auto-accept` | off | Skip the approval prompt |

## Security notes & limits (v1)

- **The code is the credential.** Anyone with the current code *and* the agent's
  approval can control the machine. Codes are single-session and disappear when
  the agent closes. Treat a code like a password; share it over a trusted channel.
- **Consent is explicit** by default (approval prompt + on-screen banner).
  `--auto-accept` removes the prompt — only use it on a machine you fully control.
- **Frames flow through the Worker.** That keeps it firewall-friendly but uses
  Worker bandwidth and adds latency versus a peer-to-peer link. WebRTC is the
  planned upgrade path; the browser UI would not change.
- Keep JPEG frames under the platform's WebSocket message size limit — the
  default `--max-width 1600` keeps typical frames well under 1 MB.
- One viewer per session in v1.

## Roadmap

- WebRTC transport (H.264/VP8 video + data channel) for lower latency.
- A native tray/GUI agent with an in-window Approve/Deny dialog.
- Clipboard sync, file transfer, multi-monitor selection.
- Optional end-to-end encryption and a short PIN in addition to the code.
- macOS / Linux agents.
