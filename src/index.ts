import { SessionDO } from "./session";

export { SessionDO };

export interface Env {
  SESSIONS: DurableObjectNamespace<SessionDO>;
  ASSETS: Fetcher;
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

    // Everything else is a static asset (the web app).
    return env.ASSETS.fetch(request);
  },
};
