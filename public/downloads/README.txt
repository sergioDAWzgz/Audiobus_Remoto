The agent binary is NOT served from this folder anymore.

The WebRTC agent (audiobus-agent.exe) is ~60 MB (it bundles PyAV/ffmpeg), which
is over Cloudflare's 25 MiB static-asset limit. It is therefore stored in an R2
bucket and served by the Worker at /downloads/audiobus-agent.exe.

To publish a build:
  1. Enable R2 on the Cloudflare account (one-time, dashboard).
  2. wrangler r2 bucket create audiobus-remote-downloads   (one-time)
  3. Build + upload:  agent/build.ps1  (builds the exe and uploads it to R2)
  4. Uncomment the "r2_buckets" binding in wrangler.jsonc, then `wrangler deploy`.

The .exe is intentionally git-ignored (large build artifact).
