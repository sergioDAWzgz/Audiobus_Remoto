import { DurableObject } from "cloudflare:workers";

const SESSION_TTL_MS = 15 * 60 * 1000; // reservation / idle lifetime

/**
 * One SessionDO instance per session code. It is the meeting point between the
 * agent (the computer being shared) and the viewer (the browser controlling it).
 *
 * It is deliberately a dumb relay:
 *   - binary messages from the agent (JPEG screen frames)  -> viewer
 *   - text/JSON messages from the viewer (input events)    -> agent
 *   - text/JSON control messages from the agent (meta etc.) -> viewer
 *
 * All consent logic (approve/deny a connection) lives in the agent, not here.
 */
export class SessionDO extends DurableObject {
  /** Claim this code for a new session. Returns false if already claimed. */
  async reserve(): Promise<boolean> {
    const reserved = await this.ctx.storage.get<boolean>("reserved");
    if (reserved) return false;
    await this.ctx.storage.put("reserved", true);
    await this.ctx.storage.setAlarm(Date.now() + SESSION_TTL_MS);
    return true;
  }

  /** Read-only: has this code been reserved by a real session? Used to gate
   *  TURN-credential minting so arbitrary well-formed codes can't drain the
   *  Cloudflare Realtime quota (the format check alone is not a real guard). */
  async isReserved(): Promise<boolean> {
    return (await this.ctx.storage.get<boolean>("reserved")) === true;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== "/connect") {
      return new Response("Not found", { status: 404 });
    }
    const role = url.searchParams.get("role");
    if (role !== "agent" && role !== "viewer") {
      return new Response("Bad role", { status: 400 });
    }

    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];

    if (role === "agent") {
      const reserved = await this.ctx.storage.get<boolean>("reserved");
      if (!reserved) {
        return new Response("Session not found or expired.", { status: 404 });
      }
      if (this.ctx.getWebSockets("agent").length > 0) {
        return new Response("An agent is already connected.", { status: 409 });
      }
      this.ctx.acceptWebSocket(server, ["agent"]);
      // Keep the session alive while the agent is connected.
      await this.ctx.storage.setAlarm(Date.now() + SESSION_TTL_MS);
      server.send(JSON.stringify({ type: "registered" }));
      return new Response(null, { status: 101, webSocket: client });
    }

    // role === "viewer": always accept, then report any problem over the socket
    // so the browser can show a friendly message (a 4xx to an upgrade request is
    // invisible to the WebSocket client).
    this.ctx.acceptWebSocket(server, ["viewer"]);
    if (this.ctx.getWebSockets("agent").length === 0) {
      server.send(
        JSON.stringify({
          type: "error",
          code: "no-agent",
          message:
            "No computer is sharing that code. Check the code and make sure the agent is running.",
        }),
      );
      server.close(4404, "no agent");
      return new Response(null, { status: 101, webSocket: client });
    }
    // getWebSockets("viewer") already includes the socket we just accepted.
    if (this.ctx.getWebSockets("viewer").length > 1) {
      server.send(
        JSON.stringify({
          type: "error",
          code: "busy",
          message: "Someone else is already controlling this computer.",
        }),
      );
      server.close(4409, "busy");
      return new Response(null, { status: 101, webSocket: client });
    }

    server.send(JSON.stringify({ type: "connected" }));
    for (const agent of this.ctx.getWebSockets("agent")) {
      try {
        agent.send(JSON.stringify({ type: "viewer-joined" }));
      } catch {
        /* ignore */
      }
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer) {
    const role = this.ctx.getTags(ws)[0];
    const targets =
      role === "agent"
        ? this.ctx.getWebSockets("viewer")
        : this.ctx.getWebSockets("agent");
    for (const target of targets) {
      try {
        target.send(message);
      } catch {
        /* peer went away mid-send */
      }
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string) {
    const role = this.ctx.getTags(ws)[0];
    if (role === "agent") {
      // The shared computer is gone: end the session for everyone.
      for (const viewer of this.ctx.getWebSockets("viewer")) {
        try {
          viewer.send(JSON.stringify({ type: "peer-left", who: "agent" }));
          viewer.close(1000, "agent disconnected");
        } catch {
          /* ignore */
        }
      }
      await this.teardown();
    } else {
      // A viewer left: tell the agent so it can stop capturing and wait again.
      for (const agent of this.ctx.getWebSockets("agent")) {
        try {
          agent.send(JSON.stringify({ type: "peer-left", who: "viewer" }));
        } catch {
          /* ignore */
        }
      }
    }
    try {
      ws.close(code, reason);
    } catch {
      /* already closing */
    }
  }

  async webSocketError(ws: WebSocket) {
    const role = this.ctx.getTags(ws)[0];
    if (role === "agent") {
      for (const viewer of this.ctx.getWebSockets("viewer")) {
        try {
          viewer.close(1011, "agent error");
        } catch {
          /* ignore */
        }
      }
      await this.teardown();
    }
  }

  async alarm() {
    // If no agent is connected when the timer fires, the session is dead.
    if (this.ctx.getWebSockets("agent").length === 0) {
      for (const s of this.ctx.getWebSockets()) {
        try {
          s.close(1001, "session expired");
        } catch {
          /* ignore */
        }
      }
      await this.teardown();
    } else {
      await this.ctx.storage.setAlarm(Date.now() + SESSION_TTL_MS);
    }
  }

  private async teardown() {
    try {
      await this.ctx.storage.deleteAlarm();
    } catch {
      /* ignore */
    }
    await this.ctx.storage.deleteAll();
  }
}
