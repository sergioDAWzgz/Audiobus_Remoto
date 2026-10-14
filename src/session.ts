import { DurableObject } from "cloudflare:workers";

const SESSION_TTL_MS = 15 * 60 * 1000; // reservation / idle lifetime
// After the agent's socket drops, hold the session (reservation + viewers) this
// long so the agent can reconnect with the SAME code before the session is torn
// down. Must comfortably exceed the client's 5s interrupt + reconnect attempts.
const AGENT_GRACE_MS = 60 * 1000;

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
      // Replace any still-registered agent socket instead of rejecting with 409. After
      // a half-open drop (Wi-Fi off, cable pulled) the runtime hasn't reaped the dead
      // socket yet, so it still shows as "connected"; a 409 would strand the agent —
      // unable to reconnect — until that ghost times out, which can outlast the whole
      // session. Only the code's owner connects (the code is a per-session secret), so
      // a new agent connection is the SAME agent reconnecting: let it take over. The
      // old socket's webSocketClose sees the replacement and skips the drop teardown.
      for (const stale of this.ctx.getWebSockets("agent")) {
        try {
          // 4000 (not a reserved 1xxx code — ws.close only allows 1000 or 3000-4999)
          // so the close actually succeeds and the stale socket leaves the registry.
          stale.close(4000, "replaced by reconnect");
        } catch {
          /* ignore */
        }
      }
      this.ctx.acceptWebSocket(server, ["agent"]);
      // Keep the session alive while the agent is connected (this also cancels any
      // short grace timer set when a previous agent socket dropped).
      await this.ctx.storage.setAlarm(Date.now() + SESSION_TTL_MS);
      // Did the controlling viewer leave while we were in the grace window? Consume
      // that flag (set by the viewer's own close while no agent was connected).
      const viewerLeftInGrace = await this.ctx.storage.get<boolean>("viewerLeftInGrace");
      if (viewerLeftInGrace) await this.ctx.storage.delete("viewerLeftInGrace");
      server.send(JSON.stringify({ type: "registered" }));
      // Reconnect: if a viewer is still connected (held through the agent's grace
      // window), re-serve it via the agent's normal viewer-joined path and tell the
      // viewer the agent is back. On a first-ever connect there is no viewer yet.
      const heldViewers = this.ctx.getWebSockets("viewer");
      if (heldViewers.length > 0) {
        try {
          // resumed:true tells the agent this is the SAME held viewer it was already
          // serving (re-serve after the agent's own reconnect) — safe to re-admit
          // without a fresh consent prompt. A normal viewer-joined has no flag.
          server.send(JSON.stringify({ type: "viewer-joined", resumed: true }));
        } catch {
          /* ignore */
        }
        for (const viewer of heldViewers) {
          try {
            viewer.send(JSON.stringify({ type: "agent-restored" }));
          } catch {
            /* ignore */
          }
        }
      } else if (viewerLeftInGrace) {
        // The viewer we were controlling for departed while we were away: tell the
        // reconnecting agent so it clears its now-stale "controlling" banner/status
        // instead of restoring a session with no viewer. This fires ONLY when a viewer
        // actually left during the grace — never on a first connect or on an idle blip
        // with no viewer, which would otherwise show a false "viewer disconnected".
        try {
          server.send(JSON.stringify({ type: "peer-left", who: "viewer" }));
        } catch {
          /* ignore */
        }
      }
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
    // Heartbeat: echo a bare "ping" straight back as "pong" to the SAME socket
    // (never relayed), so each side can measure its own round-trip to the server.
    // A plain-string compare can't collide with JSON control/input (objects) or
    // binary frames.
    if (message === "ping") {
      try {
        ws.send("pong");
      } catch {
        /* ignore */
      }
      return;
    }
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
      // If another agent socket is already registered, this close is a STALE socket
      // being replaced by a reconnect (see the fetch() agent path) — not a real drop.
      // Don't warn viewers or start the grace; the new socket is already serving.
      const liveAgents = this.ctx.getWebSockets("agent").filter((s) => s !== ws);
      if (liveAgents.length === 0) {
        // The agent's link dropped. Hold the session for a grace window so the agent
        // can reconnect with the same code; tell viewers so they can warn + wait. If
        // the agent never returns, alarm() tears the session down at the deadline.
        for (const viewer of this.ctx.getWebSockets("viewer")) {
          try {
            viewer.send(JSON.stringify({ type: "agent-interrupted" }));
          } catch {
            /* ignore */
          }
        }
        await this.ctx.storage.setAlarm(Date.now() + AGENT_GRACE_MS);
      }
    } else {
      const agents = this.ctx.getWebSockets("agent");
      if (agents.length > 0) {
        // A viewer left while the agent is connected: tell it so it stops capturing
        // and waits again.
        for (const agent of agents) {
          try {
            agent.send(JSON.stringify({ type: "peer-left", who: "viewer" }));
          } catch {
            /* ignore */
          }
        }
      } else {
        // The viewer left while the agent itself is mid-grace (dropped, reconnecting).
        // No agent socket to notify now, so remember it: when the agent reconnects it
        // is told the viewer is gone, so it won't restore a stale "controlling" state.
        await this.ctx.storage.put("viewerLeftInGrace", true);
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
      // Skip when this is a stale socket being replaced by a reconnect (see
      // webSocketClose) — another agent socket is already live and serving.
      const liveAgents = this.ctx.getWebSockets("agent").filter((s) => s !== ws);
      if (liveAgents.length > 0) return;
      // Same as a close: hold the session for the grace window and let viewers warn
      // rather than ending immediately.
      for (const viewer of this.ctx.getWebSockets("viewer")) {
        try {
          viewer.send(JSON.stringify({ type: "agent-interrupted" }));
        } catch {
          /* ignore */
        }
      }
      await this.ctx.storage.setAlarm(Date.now() + AGENT_GRACE_MS);
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
