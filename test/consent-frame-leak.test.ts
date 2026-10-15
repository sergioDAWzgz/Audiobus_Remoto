import { SELF } from "cloudflare:test";
import { describe, it, expect } from "vitest";

// ---------------------------------------------------------------------------
// e2e: the consent frame-leak during a viewer ghost-takeover (JPEG relay).
//
// These drive the REAL Worker + SessionDO in workerd. A viewer's screen frames
// (binary JPEG) must only ever be relayed to a viewer socket the agent has
// approved. The leak being guarded: when a different code-holder W replaces a
// dropped viewer V's stale ghost, the agent is briefly still streaming (it has
// not yet processed the plain viewer-joined and stopped), and those in-flight
// frames must NOT reach W before W earns consent.
//
// We assert at the SOCKET level (raw bytes received), not at the browser-render
// level: the stock viewer ignores pre-"monitors" frames, but a custom/malicious
// client with the code would still receive any relayed JPEG bytes — so the
// guarantee has to hold in the DO's relay, which is what these exercise.
// ---------------------------------------------------------------------------

const ORIGIN = "https://session.test";

/** A minimal buffering WebSocket client over SELF.fetch's upgrade response. All
 *  inbound messages are buffered (text in `texts`, binary counted in `bins`); a
 *  wake list lets `waitForType` sleep until the next message and re-scan. */
class TestWs {
  private texts: string[] = [];
  private bins: ArrayBuffer[] = [];
  private wakers: Array<() => void> = [];

  private constructor(private ws: WebSocket) {
    ws.addEventListener("message", (ev: MessageEvent) => {
      if (typeof ev.data === "string") this.texts.push(ev.data);
      // workerd delivers binary as ArrayBuffer (accepted sockets default to it).
      else this.bins.push(ev.data as ArrayBuffer);
      this.wake();
    });
  }

  static async open(path: string): Promise<TestWs> {
    const res = await SELF.fetch(`${ORIGIN}${path}`, {
      headers: { Upgrade: "websocket" },
    });
    const ws = res.webSocket;
    if (!ws) throw new Error(`expected a WebSocket upgrade, got HTTP ${res.status}`);
    // Attach the listener BEFORE accept(): the DO sends "registered"/"connected"
    // immediately on accept, and messages that arrive before a listener exists are
    // dropped. The constructor wires the handler; only then do we start the socket.
    const client = new TestWs(ws);
    ws.accept();
    return client;
  }

  send(obj: unknown): void {
    this.ws.send(JSON.stringify(obj));
  }

  sendBinary(bytes: Uint8Array): void {
    // Copy into a standalone ArrayBuffer so we send exactly these bytes.
    this.ws.send(bytes.slice().buffer);
  }

  /** Resolve with the next buffered text message whose parsed `type` is `type`,
   *  removing it from the buffer. Rejects if none arrives within `ms`. */
  async waitForType(type: string, ms = 2000): Promise<any> {
    const deadline = Date.now() + ms;
    for (;;) {
      const idx = this.texts.findIndex((t) => this.parsedType(t) === type);
      if (idx !== -1) {
        const [msg] = this.texts.splice(idx, 1);
        return JSON.parse(msg);
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) {
        throw new Error(
          `timed out waiting for "${type}"; buffered: ${JSON.stringify(this.texts)}`,
        );
      }
      await this.waitWake(remaining);
    }
  }

  private parsedType(t: string): string | undefined {
    try {
      return JSON.parse(t)?.type;
    } catch {
      return undefined;
    }
  }

  private wake(): void {
    const pending = this.wakers;
    this.wakers = [];
    for (const w of pending) w();
  }

  private waitWake(ms: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        const i = this.wakers.indexOf(waker);
        if (i !== -1) this.wakers.splice(i, 1);
        resolve();
      }, ms);
      const waker = () => {
        clearTimeout(timer);
        resolve();
      };
      this.wakers.push(waker);
    });
  }

  /** Number of binary frames received so far. */
  binaryCount(): number {
    return this.bins.length;
  }
}

async function newSessionCode(): Promise<string> {
  const res = await SELF.fetch(`${ORIGIN}/api/new-session`, { method: "POST" });
  expect(res.status).toBe(200);
  const body = (await res.json()) as { code: string };
  expect(body.code).toMatch(/^[0-9A-Z]{10}$/);
  return body.code;
}

const frame = (monitor = 0) => new Uint8Array([monitor, 0xff, 0xd8, 0xff, 0xd9]);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Settle delay: let the Worker/DO deliver already-sent messages before asserting.
const settle = () => sleep(200);

describe("JPEG relay consent gating", () => {
  it("relays screen frames to an approved viewer", async () => {
    const code = await newSessionCode();
    const agent = await TestWs.open(`/ws/agent?code=${code}`);
    await agent.waitForType("registered");

    const viewer = await TestWs.open(`/ws/viewer?code=${code}`);
    await viewer.waitForType("connected");
    const joined = await agent.waitForType("viewer-joined");
    expect(joined.vid).toBeTruthy();

    // Agent approves this viewer, then streams.
    agent.send({ type: "serving", vid: joined.vid });
    agent.send({ type: "monitors", monitors: [{ i: 0, w: 100, h: 100 }] });
    await viewer.waitForType("monitors");

    agent.sendBinary(frame());
    agent.sendBinary(frame());
    await settle();

    expect(viewer.binaryCount()).toBe(2);
  });

  it("does NOT leak in-flight frames to a viewer that replaced a ghost until it is re-served", async () => {
    const code = await newSessionCode();
    const agent = await TestWs.open(`/ws/agent?code=${code}`);
    await agent.waitForType("registered");

    // Viewer V connects and is approved; frames flow to V.
    const v = await TestWs.open(`/ws/viewer?code=${code}`);
    await v.waitForType("connected");
    const joinedV = await agent.waitForType("viewer-joined");
    agent.send({ type: "serving", vid: joinedV.vid });
    agent.send({ type: "monitors", monitors: [{ i: 0, w: 100, h: 100 }] });
    await v.waitForType("monitors");
    agent.sendBinary(frame());
    await settle();
    expect(v.binaryCount()).toBe(1);

    // V's link goes silent (a frozen tab / half-open drop): it simply stops pinging.
    // Wait past SILENCE_MS (5s in the DO) so V is seen as a dead ghost, letting a new
    // code-holder take over its slot instead of being refused "busy".
    await sleep(5500);

    // A DIFFERENT code-holder W takes over V's ghost. The agent is still streaming
    // (it has NOT yet processed W's viewer-joined): keep sending frames.
    const w = await TestWs.open(`/ws/viewer?code=${code}`);
    await w.waitForType("connected");
    const joinedW = await agent.waitForType("viewer-joined");
    expect(joinedW.vid).toBeTruthy();
    expect(joinedW.vid).not.toBe(joinedV.vid); // a genuinely new viewer socket
    expect(joinedW.resumed).toBeFalsy(); // plain viewer-joined: W must re-earn consent

    // In-flight frames during the gap before the agent re-serves W.
    agent.sendBinary(frame());
    agent.sendBinary(frame());
    agent.sendBinary(frame());
    await settle();

    // THE FIX: W must have received none of those frames.
    expect(w.binaryCount()).toBe(0);

    // Now the agent processes W's viewer-joined and (after consent) re-serves it.
    agent.send({ type: "serving", vid: joinedW.vid });
    agent.send({ type: "monitors", monitors: [{ i: 0, w: 100, h: 100 }] });
    await w.waitForType("monitors");
    agent.sendBinary(frame());
    await settle();

    // Only after the explicit re-serve do frames reach W.
    expect(w.binaryCount()).toBe(1);
  }, 20000);

  it("stays backward compatible: an agent that never asserts `serving` gets relay-to-all", async () => {
    const code = await newSessionCode();
    const agent = await TestWs.open(`/ws/agent?code=${code}`);
    await agent.waitForType("registered");

    const viewer = await TestWs.open(`/ws/viewer?code=${code}`);
    await viewer.waitForType("connected");
    await agent.waitForType("viewer-joined");

    // A legacy agent: it sends monitors and streams WITHOUT a `serving` assertion.
    agent.send({ type: "monitors", monitors: [{ i: 0, w: 100, h: 100 }] });
    await viewer.waitForType("monitors");
    agent.sendBinary(frame());
    agent.sendBinary(frame());
    await settle();

    // Frames still flow (the DO falls back to relay-to-all for such agents).
    expect(viewer.binaryCount()).toBe(2);
  });
});
