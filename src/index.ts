import { SessionDO } from "./session";

export { SessionDO };

export interface Env {
  SESSIONS: DurableObjectNamespace<SessionDO>;
  ASSETS: Fetcher;
  // Cloudflare Realtime TURN. Both are secrets (`wrangler secret put TURN_KEY_ID`
  // and `... TURN_API_TOKEN`); when either is missing the app falls back to
  // STUN-only (works on permissive networks).
  TURN_KEY_ID?: string;
  TURN_API_TOKEN?: string;
  // R2 bucket holding the agent binary (it exceeds the 25 MiB Workers static-asset
  // limit, so it can't live in /public). Optional: when the binding is absent the
  // download route is dormant and falls through to static assets.
  DOWNLOADS?: R2Bucket;
}

// Fallback used whenever TURN credentials can't be minted: STUN lets WebRTC work
// on permissive networks (home routers) but not on symmetric-NAT/corporate ones.
const STUN_ONLY = [{ urls: ["stun:stun.cloudflare.com:3478"] }];

// Mint short-lived ICE servers (STUN + TURN) for a peer connection. Both the
// agent and the viewer call this just before creating their RTCPeerConnection.
async function handleIceServers(env: Env): Promise<Response> {
  const keyId = env.TURN_KEY_ID;
  const token = env.TURN_API_TOKEN;
  if (!keyId || !token) {
    return Response.json({ iceServers: STUN_ONLY, turn: false });
  }
  try {
    const resp = await fetch(
      `https://rtc.live.cloudflare.com/v1/turn/keys/${keyId}/credentials/generate-ice-servers`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ttl: 3600 }),
      },
    );
    if (!resp.ok) {
      return Response.json({ iceServers: STUN_ONLY, turn: false });
    }
    const data = (await resp.json()) as { iceServers?: unknown };
    if (!Array.isArray(data.iceServers) || data.iceServers.length === 0) {
      return Response.json({ iceServers: STUN_ONLY, turn: false });
    }
    return Response.json({ iceServers: data.iceServers, turn: true });
  } catch {
    return Response.json({ iceServers: STUN_ONLY, turn: false });
  }
}

// Session code alphabet: uppercase letters + digits, minus the visually
// ambiguous ones (0/O, 1/I/L). Still alphanumeric, just easier to read aloud.
const CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";
const CODE_LENGTH = 10;

function generateCode(): string {
  const bytes = new Uint8Array(CODE_LENGTH);
  crypto.getRandomValues(bytes);
  let out = "";
  for (let i = 0; i < CODE_LENGTH; i++) {
    out += CODE_ALPHABET[bytes[i] % CODE_ALPHABET.length];
  }
  return out;
}

function isValidCode(code: string): boolean {
  if (code.length !== CODE_LENGTH) return false;
  for (const ch of code) {
    if (!CODE_ALPHABET.includes(ch)) return false;
  }
  return true;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // The agent asks for a fresh, unused session code.
    if (path === "/api/new-session" && request.method === "POST") {
      for (let attempt = 0; attempt < 5; attempt++) {
        const code = generateCode();
        const stub = env.SESSIONS.getByName(code);
        const reserved = await stub.reserve();
        if (reserved) {
          return Response.json({
            code,
            ws: `/ws/agent?code=${code}`,
          });
        }
      }
      return new Response("Could not allocate a session, please retry.", {
        status: 503,
      });
    }

    // ICE servers (STUN + minted TURN) for WebRTC. Minting a TURN credential costs
    // real bandwidth/quota, so it is gated on a code that is actually reserved by a
    // live session (not merely well-formed) — otherwise any caller could harvest
    // credentials. Both the agent (which reserved the code) and the viewer (which
    // only connects to a shared code) hold a reserved code when they ask.
    if (path === "/api/ice-servers") {
      const code = (url.searchParams.get("code") || "").toUpperCase();
      if (!isValidCode(code)) {
        return new Response("Invalid session code.", { status: 400 });
      }
      const stub = env.SESSIONS.getByName(code);
      if (!(await stub.isReserved())) {
        return new Response("Session not found or expired.", { status: 404 });
      }
      return handleIceServers(env);
    }

    // WebSocket endpoints for the agent (screen owner) and the viewer (browser).
    if (path === "/ws/agent" || path === "/ws/viewer") {
      if (request.headers.get("Upgrade") !== "websocket") {
        return new Response("Expected a WebSocket upgrade.", { status: 426 });
      }
      const code = (url.searchParams.get("code") || "").toUpperCase();
      if (!isValidCode(code)) {
        return new Response("Invalid session code.", { status: 400 });
      }
      const role = path === "/ws/agent" ? "agent" : "viewer";
      const stub = env.SESSIONS.getByName(code);
      const doReq = new Request(`https://session/connect?role=${role}`, request);
      return stub.fetch(doReq);
    }

    // The agent binary is served from R2 (it is larger than the 25 MiB Workers
    // static-asset limit). Dormant until the DOWNLOADS binding is configured; then
    // the /share download link (same path) is served from the bucket.
    if (path === "/downloads/audiobus-agent.exe" && env.DOWNLOADS) {
      const obj = await env.DOWNLOADS.get("audiobus-agent.exe");
      if (!obj) {
        return new Response("The agent build isn't available yet.", { status: 404 });
      }
      const headers = new Headers();
      obj.writeHttpMetadata(headers);
      headers.set("content-type", "application/octet-stream");
      headers.set("content-disposition", 'attachment; filename="audiobus-agent.exe"');
      headers.set("content-length", obj.size.toString());
      headers.set("etag", obj.httpEtag);
      return new Response(obj.body, { headers });
    }

    // Everything else is a static asset (the web app).
    return env.ASSETS.fetch(request);
  },
};
