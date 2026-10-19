import { DurableObject } from "cloudflare:workers";

const SESSION_TTL_MS = 15 * 60 * 1000; // reservation / idle lifetime
// After the agent's socket drops, hold the session (reservation + viewers) this
// long so the agent can reconnect with the SAME code before the session is torn
// down. Must comfortably exceed the client's 5s interrupt + reconnect attempts.
const AGENT_GRACE_MS = 60 * 1000;
// Each side app-pings ~once a second. If a side goes silent for this long its link
// has gone quiet. The two directions are handled ASYMMETRICALLY:
//   - A silent AGENT (native app, pings reliably) is a genuine half-open drop: the
//     viewer's next ping closes it so webSocketClose warns the viewer (agent-
//     interrupted + grace).
//   - A silent VIEWER (browser) is NOT reaped: a tab's timers legitimately freeze
//     for seconds-to-minutes (background tab, device sleep, intensive throttling) with
//     the socket still open. Closing it would force a needless reconnect + fresh
//     consent. Instead the agent is sent an advisory (viewer-interrupted) so it can
//     warn its user, leaving stream + consent intact; a real departure still arrives
//     as a webSocketClose (-> peer-left), and a dead viewer's reconnect is replaced.
// Comfortably above the 1s ping interval and the client's own 3s interrupt window.
const SILENCE_MS = 5000;
// A silent viewer is advised, not reaped — but if it stays CONTINUOUSLY silent far
// longer than any benign freeze (Chrome intensive throttling still pings ~1/min, so a
// backgrounded-but-alive tab is never silent this long), give up: close its ghost so
// webSocketClose reports a clean "viewer left" and the agent stops streaming to a
// socket that will never read again. Long enough not to fire on a normal background tab.
const VIEWER_GIVEUP_MS = 90 * 1000;

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
  /** When the viewer's link first went continuously silent (undefined = not silent),
   *  so we can give up on a ghost that never comes back (VIEWER_GIVEUP_MS). In-memory:
   *  reset on hibernation at worst restarts the give-up clock, which is harmless. */
  private viewerSilentSince: number | undefined = undefined;

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
      // Stamp a baseline lastPing so silence checks have a reference before the first
      // real ping arrives (a freshly-accepted socket isn't mistaken for a ghost).
      try {
        server.serializeAttachment({ lastPing: Date.now() });
      } catch {
        /* ignore */
      }
      // Keep the session alive while the agent is connected (this also cancels any
      // short grace timer set when a previous agent socket dropped).
      await this.ctx.storage.setAlarm(Date.now() + SESSION_TTL_MS);
      // Did the controlling viewer leave while we were in the grace window? Consume
      // that flag (set by the viewer's own close while no agent was connected).
      const viewerLeftInGrace = await this.ctx.storage.get<boolean>("viewerLeftInGrace");
      if (viewerLeftInGrace) await this.ctx.storage.delete("viewerLeftInGrace");
      server.send(JSON.stringify({ type: "registered" }));
      // Reconnect: if a viewer is still connected AND LIVE (held through the agent's
      // grace window, still pinging), re-serve it as resumed and tell it the agent is
      // back. resumed:true auto-re-admits WITHOUT a fresh prompt, so it must only ever
      // name a viewer that is genuinely the same one: a LIVE viewer cannot have been
      // taken over (a second viewer is refused busy while the incumbent is live), so
      // resuming a live held viewer is safe. A held viewer that has gone silent (a dead
      // ghost) is NOT resumed — a different code-holder could replace that ghost, and
      // resuming it would hand them the previous viewer's approval. We treat a ghost-
      // only hold as "viewer gone" (peer-left), so any later reconnect re-earns consent.
      const now = Date.now();
      const heldViewers = this.ctx.getWebSockets("viewer");
      let heldVid: string | undefined;
      let heldVtok: string | undefined;
      const liveHeld = heldViewers.filter((v) => {
        let att: { lastPing?: number; vid?: string; vtok?: string } | null = null;
        try {
          att = v.deserializeAttachment() as
            | { lastPing?: number; vid?: string; vtok?: string }
            | null;
        } catch {
          /* ignore */
        }
        const last = att?.lastPing;
        const live = last !== undefined && now - last <= SILENCE_MS;
        if (live && heldVid === undefined) {
          heldVid = att?.vid;
          heldVtok = att?.vtok;
        }
        return live;
      });
      if (liveHeld.length > 0) {
        try {
          // vid names WHICH viewer this resumes. The agent auto-re-admits on resumed
          // only if this vid matches the one it approved — so a live held viewer that is
          // live because it REPLACED a ghost (a different code-holder, while the agent
          // was also away and never saw the plain viewer-joined that would reset consent)
          // carries a NEW vid and is re-prompted, not silently inherited. The viewer's
          // stable resume token rides along too, for the viewer's-own-reconnect path.
          server.send(
            JSON.stringify({ type: "viewer-joined", resumed: true, vid: heldVid, vtok: heldVtok }),
          );
        } catch {
          /* ignore */
        }
        for (const viewer of liveHeld) {
          try {
            viewer.send(JSON.stringify({ type: "agent-restored" }));
          } catch {
            /* ignore */
          }
        }
      } else if (viewerLeftInGrace || heldViewers.length > 0) {
        // Either the viewer we were controlling for departed while we were away
        // (viewerLeftInGrace), or the only held viewer(s) are dead ghosts (silent, not
        // resumed above). Tell the reconnecting agent the viewer is gone so it clears
        // its now-stale "controlling" state and re-earns consent for whoever reconnects
        // next, rather than restoring a session with no (live) viewer. This never fires
        // on a first connect or an idle blip with no viewer at all.
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
    // Tag this viewer socket with a unique id. It is server-side only (never sent to
    // the viewer) and travels to the agent in viewer-joined, so the agent can tell
    // whether a resumed re-serve is the SAME viewer it approved or a different one that
    // took over the slot. crypto.randomUUID is available in the Workers runtime.
    const vid = crypto.randomUUID();
    // The viewer's own stable RESUME TOKEN (minted and held by its browser, sent on
    // every connect). Unlike vid (per-socket, changes on reconnect) it is constant for a
    // browser across reconnects, so the agent can recognise the SAME approved viewer
    // returning on a fresh socket (own-link drop / dual-drop) and resume WITHOUT a new
    // prompt. It is opaque to us and unguessable; a different code-holder carries a
    // different token and still earns consent.
    const vtok = url.searchParams.get("vtok") || undefined;
    try {
      server.serializeAttachment({ lastPing: Date.now(), vid, vtok });
    } catch {
      /* ignore */
    }
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
    // Is a viewer already here (besides the one we just accepted)? Distinguish a live
    // controller from a dead ghost. A reconnecting viewer whose old socket half-open
    // dropped takes over its OWN ghost (the code is a per-session secret); a genuinely
    // second viewer (incumbent still pinging) is refused as busy, preserving the
    // single-controller guarantee.
    const incumbents = this.ctx.getWebSockets("viewer").filter((v) => v !== server);
    if (incumbents.length > 0) {
      const now = Date.now();
      let busyIncumbent = false;
      for (const v of incumbents) {
        let att: { lastPing?: number; vtok?: string } | null = null;
        try {
          att = v.deserializeAttachment() as { lastPing?: number; vtok?: string } | null;
        } catch {
          /* no attachment */
        }
        const last = att?.lastPing;
        const live = last === undefined || now - last <= SILENCE_MS;
        // A live incumbent with the SAME resume token is THIS viewer reconnecting faster
        // than its old socket went silent — take over its own ghost. A live incumbent
        // with a different/absent token is a genuine second controller → busy, preserving
        // the single-controller guarantee against anyone else holding the code.
        const sameViewer = vtok !== undefined && att?.vtok === vtok;
        if (live && !sameViewer) busyIncumbent = true;
      }
      if (busyIncumbent) {
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
      // Every incumbent is either a silent ghost (half-open dropped) or this same
      // viewer's own earlier socket (matching resume token): replace them. Their
      // webSocketClose skips the peer-left (another viewer is now live).
      for (const v of incumbents) {
        try {
          v.close(4000, "replaced by reconnect");
        } catch {
          /* ignore */
        }
      }
    }
    // A fresh or replacement viewer: clear any pending "viewer interrupted" advisory.
    await this.ctx.storage.delete("viewerWarned");
    this.viewerSilentSince = undefined;

    server.send(JSON.stringify({ type: "connected" }));
    // Always a PLAIN viewer-joined (never resumed) carrying this viewer's vid. This
    // viewer arrived on a NEW socket (a first connect or a reconnect/takeover of a
    // ghost), and anyone holding the code could be on the other end, so it must re-earn
    // consent; the agent records this vid as the approved one iff the owner accepts.
    // Only the agent's OWN reconnect re-serves a still-held viewer silently (resumed),
    // and only when the resumed vid matches — see the agent branch above.
    for (const agent of this.ctx.getWebSockets("agent")) {
      try {
        agent.send(JSON.stringify({ type: "viewer-joined", vid, vtok }));
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
      // Liveness tracking. Stamp this socket's last ping, then judge the peer — but
      // handle the two directions asymmetrically (see SILENCE_MS).
      const pingRole = this.ctx.getTags(ws)[0];
      const now = Date.now();
      // Was THIS socket itself silent until just now? Then it just woke from its own
      // suspension (device sleep, tab freeze) and must not judge the peer this round:
      // the peer may simply not have had a chance to re-ping yet (a shared suspension
      // would otherwise have the first waker kill the still-recovering other side).
      let ownAtt: { lastPing?: number; vid?: string } | null = null;
      try {
        ownAtt = ws.deserializeAttachment() as { lastPing?: number; vid?: string } | null;
      } catch {
        /* no attachment yet */
      }
      const prevOwn = ownAtt?.lastPing;
      try {
        // Preserve the rest of the attachment (notably a viewer's vid) across the stamp.
        ws.serializeAttachment({ ...(ownAtt || {}), lastPing: now });
      } catch {
        /* ignore */
      }
      if (prevOwn !== undefined && now - prevOwn > SILENCE_MS) return;

      if (pingRole === "viewer") {
        // The viewer is checking the AGENT. The agent is the native app and pings
        // reliably, so a silent agent is a genuine half-open drop (no close reached
        // us): close it so webSocketClose warns the viewer (agent-interrupted + grace).
        for (const agent of this.ctx.getWebSockets("agent")) {
          let last: number | undefined;
          try {
            last = (agent.deserializeAttachment() as { lastPing?: number } | null)?.lastPing;
          } catch {
            /* ignore */
          }
          if (last !== undefined && now - last > SILENCE_MS) {
            try {
              agent.close(4001, "peer silent");
            } catch {
              /* ignore */
            }
          }
        }
        return;
      }

      // The agent is checking the VIEWER. A browser viewer's timers legitimately
      // freeze for seconds-to-minutes with the socket still open, so a silent viewer
      // is NOT proof it's gone and we must NOT close its socket (that would force a
      // needless reconnect + fresh consent for a viewer that would have resumed on
      // its own socket). Instead ADVISE the agent so it can warn its user, leaving
      // the stream and consent intact. If the viewer pings again we clear the warning;
      // a genuine departure still arrives as a webSocketClose (-> peer-left), and a
      // truly-dead viewer's reconnect is taken over by replace.
      const viewers = this.ctx.getWebSockets("viewer");
      let anyLive = false;
      const silent: WebSocket[] = [];
      for (const v of viewers) {
        let last: number | undefined;
        try {
          last = (v.deserializeAttachment() as { lastPing?: number } | null)?.lastPing;
        } catch {
          /* ignore */
        }
        if (last !== undefined && now - last > SILENCE_MS) silent.push(v);
        else anyLive = true;
      }
      // viewerWarned lives in storage (not an in-memory field) so a DO eviction /
      // redeploy mid-interruption doesn't lose it and strand the agent on "interrupted"
      // with no matching "restored". The give-up clock may stay in memory — losing it
      // only restarts the (long) backstop, which is harmless.
      const warned = (await this.ctx.storage.get<boolean>("viewerWarned")) === true;
      if (silent.length > 0 && !anyLive) {
        // The viewer's link is quiet. Advise the agent once (keep the socket — a frozen
        // tab resumes on it), and remember when the silence began.
        if (this.viewerSilentSince === undefined) this.viewerSilentSince = now;
        if (!warned) {
          await this.ctx.storage.put("viewerWarned", true);
          try {
            ws.send(JSON.stringify({ type: "viewer-interrupted" }));
          } catch {
            /* ignore */
          }
        }
        // Backstop: continuously silent far beyond any benign freeze → give up and
        // close the ghost; its webSocketClose then reports a clean peer-left.
        if (now - this.viewerSilentSince > VIEWER_GIVEUP_MS) {
          this.viewerSilentSince = undefined;
          for (const v of silent) {
            try {
              v.close(4001, "peer gone");
            } catch {
              /* ignore */
            }
          }
        }
      } else if (anyLive) {
        this.viewerSilentSince = undefined;
        if (warned) {
          await this.ctx.storage.delete("viewerWarned");
          try {
            ws.send(JSON.stringify({ type: "viewer-restored" }));
          } catch {
            /* ignore */
          }
        }
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
        const heldViewers = this.ctx.getWebSockets("viewer");
        for (const viewer of heldViewers) {
          try {
            viewer.send(JSON.stringify({ type: "agent-interrupted" }));
          } catch {
            /* ignore */
          }
        }
        // Hold the reservation so the agent can reconnect with the SAME code after a
        // transient worker-link loss. With a viewer held, bound the hold to the short
        // AGENT_GRACE (the viewer can't wait forever). Idle (no viewer) — e.g. the
        // agent's internet dropped before anyone connected — keep the reservation for
        // the full idle TTL so its code survives a longer outage.
        await this.ctx.storage.setAlarm(
          Date.now() + (heldViewers.length > 0 ? AGENT_GRACE_MS : SESSION_TTL_MS),
        );
      }
    } else {
      // The viewer set is changing — drop any pending "viewer interrupted" advisory.
      await this.ctx.storage.delete("viewerWarned");
      this.viewerSilentSince = undefined;
      // If another viewer socket is already live, this close is a stale ghost being
      // replaced by a reconnect (see the fetch viewer path) — the new socket is
      // already serving, so don't tell the agent the viewer left.
      const liveViewers = this.ctx.getWebSockets("viewer").filter((s) => s !== ws);
      if (liveViewers.length > 0) {
        /* replaced ghost: no notification */
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
      // Same as a close: hold the session so the agent can reconnect with the same
      // code, and let any viewer warn rather than ending immediately. Idle (no viewer)
      // holds for the full idle TTL so the code survives a longer outage.
      const heldViewers = this.ctx.getWebSockets("viewer");
      for (const viewer of heldViewers) {
        try {
          viewer.send(JSON.stringify({ type: "agent-interrupted" }));
        } catch {
          /* ignore */
        }
      }
      await this.ctx.storage.setAlarm(
        Date.now() + (heldViewers.length > 0 ? AGENT_GRACE_MS : SESSION_TTL_MS),
      );
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
