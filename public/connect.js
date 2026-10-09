// Audiobus Remote — browser controller (viewer side).
// Protocol v3 (WebRTC): after consent the agent sends a one-time "monitors"
// message, then a "webrtc-offer" carrying one send-only VP8 video track per
// monitor. The viewer answers and attaches each incoming track to that monitor's
// <video>. Everything else — consent, the "want" monitor selection, input
// (mouse/keyboard), "blockrects", and session control — still travels over the
// WebSocket (relayed by the Durable Object); WebRTC carries video only.
//
// Input coordinates are sent in each monitor's FULL pixel space (from "monitors"),
// independent of the encoded video resolution, so the agent's input handler is
// unchanged from the JPEG protocol.

const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const joinEl = $("join");
const stageEl = $("stage");
const screensEl = $("screens");
const kbToggle = $("kbToggle");
const fsBtn = $("fsBtn");
const disconnectBtn = $("disconnectBtn");
const viewControls = $("viewControls");
const gridBtn = $("gridBtn");
const singleBtn = $("singleBtn");
const monitorPicker = $("monitorPicker");
const fsBar = $("fsBar");
const fsExitBtn = $("fsExitBtn");
const fsDisconnectBtn = $("fsDisconnectBtn");
const fsHint = $("fsHint");
const barActions = document.querySelector(".bar-actions");
const kbLabel = document.querySelector(".bar-actions .toggle");
const topBar = document.querySelector(".bar");
const joinStatusSlot = $("joinStatusSlot");

// Translation helper (i18n.js loads first; falls back to the key if missing).
const t = (k) => (window.I18N ? window.I18N.t(k) : k);

let ws = null;
let monitors = []; // [{ i, w, h }, ...] in the order the agent sent them
let screens = new Map(); // monitor index -> { el, handlers, mon }
let inSession = false;
let viewMode = "grid"; // "grid" | "single"
let selectedIndex = 0; // monitor index shown in single mode
let activeDown = null; // { el, mon } held between mousedown and mouseup
let windowBound = false;
let fsActive = false; // the fullscreen hover-bar is live
let modalOpen = false; // a confirmation dialog is open (suppresses remote input)
let dismissModal = null; // close() of the open confirmation dialog, or null
let currentCode = null; // the session code (needed to fetch ICE servers)
let pc = null; // RTCPeerConnection carrying the per-monitor video tracks
let trackMids = {}; // offer m-line "mid" -> monitor index (from the agent)
// ---- Connection heartbeat + interruption / active reconnection ----
const PING_MS = 2000; // send a heartbeat ping this often
const INTERRUPT_MS = 5000; // no pong for this long -> the server link is interrupted
const WARN_DELAY_MS = 5000; // reconnect starts immediately; warn only if still down this long
const REASK_MS = 10000; // after "Wait", re-ask this long later if still interrupted
const MAX_RECONNECT_MS = 65000; // > the DO's agent grace (60s): give up after this, end cleanly
let live = false; // true from connect() until endSession()
let lastPong = 0; // performance.now() of the last "pong" from the server
let lastTick = 0; // performance.now() of the previous heartbeat tick (to spot suspensions)
let hbTimer = null; // heartbeat interval id
let agentDown = false; // the AGENT's link to the server dropped (relayed by the DO)
let interruptionActive = false; // an interruption (reconnecting; maybe dialog) is in progress
let interruptStart = 0; // performance.now() when the current interruption began
let dialogTimer = null; // delay before the warning dialog first appears
let reaskTimer = null; // delay before re-asking after "Keep waiting"
let mediaFailTimer = null; // confirms a media-only WebRTC failure before ending
let reconnecting = false; // actively trying to reopen our WS
let reconnectTimer = null;
let intentionalClose = false; // we closed the WS on purpose (reconnect / endSession)

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

// The status box lives below the center card while on the join/connecting screen,
// and in the top-left bar once a session is live.
function placeStatusInJoin() {
  if (statusEl.parentElement !== joinStatusSlot) joinStatusSlot.appendChild(statusEl);
}
function placeStatusInBar() {
  if (statusEl.parentElement !== topBar) topBar.insertBefore(statusEl, barActions);
}
placeStatusInJoin(); // start on the join screen

function showJoinError(msg) {
  const e = $("joinError");
  e.textContent = msg;
  e.hidden = false;
}

$("joinForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const code = $("codeInput").value.trim().toUpperCase();
  if (code.length !== 10) {
    showJoinError(t("connect.err.codeLen"));
    return;
  }
  $("joinError").hidden = true;
  connect(code);
});

function connect(code) {
  currentCode = code;
  live = true;
  setStatus(t("connect.status.connecting"));
  openWs(code);
  startHeartbeat();
}

// (Re)open the viewer WebSocket. Used by connect() and by the reconnection loop.
function openWs(code) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  intentionalClose = false;
  ws = new WebSocket(`${proto}://${location.host}/ws/viewer?code=${code}`);
  ws.onopen = () => {
    lastPong = performance.now();
    if (!interruptionActive) {
      setStatus(
        inSession ? t("connect.status.connected") : t("connect.status.waitingAccept"),
        inSession ? "good" : "warn",
      );
    }
  };
  ws.onmessage = onMessage;
  ws.onclose = onWsClosed;
  ws.onerror = () => {};
}

function send(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}

// ---------------- Messages ----------------

async function onMessage(ev) {
  if (typeof ev.data !== "string") return; // video now travels over WebRTC
  if (ev.data === "pong") {
    lastPong = performance.now(); // heartbeat: our link to the server is alive
    return;
  }
  let msg;
  try {
    msg = JSON.parse(ev.data);
  } catch {
    return;
  }
  switch (msg.type) {
    case "connected":
      break;
    case "monitors":
      // A fresh monitor list means the agent (re)served us: the link is healthy.
      agentDown = false;
      startSession(Array.isArray(msg.monitors) ? msg.monitors : []);
      break;
    case "agent-interrupted":
      // The agent's own link to the server dropped; our link is fine. Warn + wait
      // for the agent to reconnect (the DO holds the session during its grace).
      agentDown = true;
      onInterruption();
      break;
    case "agent-restored":
      agentDown = false; // the agent reconnected; a fresh offer will follow
      break;
    case "webrtc-offer":
      await handleOffer(msg);
      break;
    case "webrtc-ice":
      await handleRemoteIce(msg);
      break;
    case "denied":
      setStatus(t("connect.status.declined"), "bad");
      endSession(true);
      break;
    case "error": {
      // During a reconnection the agent may not be back yet (no-agent / busy): keep
      // retrying instead of giving up. The DO closes this socket; tryReconnect opens
      // another, and once the agent returns the re-serve completes recovery.
      if (interruptionActive) break;
      setStatus(t("connect.status.couldNotConnect"), "bad");
      // Prefer a translated message by the error's code; fall back to the
      // server-provided English text.
      const byCode =
        msg.code === "no-agent"
          ? t("connect.err.noAgent")
          : msg.code === "busy"
            ? t("connect.err.busy")
            : null;
      if (!joinEl.hidden) {
        showJoinError(byCode || msg.message || t("connect.status.couldNotConnect"));
      }
      // Terminal join failure (no-agent / busy): stop cleanly so the DO's follow-up
      // close isn't mistaken for an interruption (which would pop the reconnect dialog).
      abortConnect();
      break;
    }
    case "peer-left":
      setStatus(t("connect.status.otherDisconnected"), "bad");
      endSession(true);
      break;
    case "session-ended":
      setStatus(t("connect.status.endedByOther"), "bad");
      endSession(true);
      break;
    case "blockrects":
      // Where the agent's own windows are (per monitor, local pixels): clicks there
      // are ignored by the agent, so show a "blocked" cursor over them.
      blockRects = Array.isArray(msg.rects) ? msg.rects : [];
      break;
  }
}

// Agent-window regions where input is blocked (per monitor), for the cursor hint.
let blockRects = [];
function inBlockRect(m, x, y) {
  for (const r of blockRects) {
    if (r.m === m && x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h) {
      return true;
    }
  }
  return false;
}

// ---------------- WebRTC ----------------

// Fetch STUN (+ TURN when configured) from the Worker. Falls back to public-ish
// STUN so a peer connection can still be attempted if the endpoint fails.
async function fetchIceServers() {
  try {
    const r = await fetch(`/api/ice-servers?code=${encodeURIComponent(currentCode || "")}`);
    if (r.ok) {
      const data = await r.json();
      if (Array.isArray(data.iceServers) && data.iceServers.length) {
        return data.iceServers;
      }
    }
  } catch {
    /* fall through to STUN-only */
  }
  return [{ urls: ["stun:stun.cloudflare.com:3478"] }];
}

async function handleOffer(msg) {
  const iceServers = await fetchIceServers();
  if (!inSession) return; // the session ended while we were fetching
  closePeer(); // never keep a stale connection (this also resets trackMids)
  // Map each media section (its SDP "mid") to the monitor it carries. Set AFTER
  // closePeer(), which clears trackMids, so the map is live when ontrack fires.
  trackMids = msg && msg.mids ? msg.mids : {};

  pc = new RTCPeerConnection({ iceServers });

  pc.ontrack = (e) => {
    const mid = e.transceiver && e.transceiver.mid;
    let idx =
      mid != null && trackMids[mid] != null ? Number(trackMids[mid]) : NaN;
    if (!Number.isInteger(idx)) idx = firstUnassignedMonitor();
    const s = screens.get(idx);
    if (!s) return;
    // One track per monitor: wrap THIS track alone (the agent groups all tracks
    // into one stream, so e.streams[0] would put every monitor on every <video>).
    s.el.srcObject = new MediaStream([e.track]);
    if (s.el.play) s.el.play().catch(() => {});
  };

  pc.onicecandidate = (e) => {
    if (e.candidate) send({ type: "webrtc-ice", candidate: e.candidate });
  };

  // The WebSocket heartbeat is the primary liveness signal (a network drop stalls it
  // and drives reconnection). But a MEDIA-ONLY failure (UDP/TURN dies while the WS
  // stays healthy) wouldn't stall the heartbeat, so catch a terminal pc "failed"
  // here — delayed briefly and guarded so it doesn't fight a WS reconnect already in
  // flight, and so a transient ICE "disconnected" that recovers won't nag.
  pc.onconnectionstatechange = () => {
    if (!inSession || !pc) return;
    const st = pc.connectionState;
    if (st === "connected") {
      if (mediaFailTimer) {
        clearTimeout(mediaFailTimer);
        mediaFailTimer = null;
      }
    } else if (st === "failed") {
      if (mediaFailTimer) clearTimeout(mediaFailTimer);
      mediaFailTimer = setTimeout(() => {
        mediaFailTimer = null;
        // End only on a true media-ONLY failure (WS healthy, no interruption in
        // flight); a full network drop is the heartbeat/reconnect flow's job.
        if (
          inSession && pc && pc.connectionState === "failed" &&
          !interruptionActive && !isDown()
        ) {
          setStatus(t("connect.status.lost"), "bad");
          endSession(true);
        }
      }, WARN_DELAY_MS);
    }
  };

  try {
    await pc.setRemoteDescription({ type: "offer", sdp: msg.sdp });
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    send({ type: "webrtc-answer", sdp: pc.localDescription.sdp });
  } catch {
    if (inSession) {
      setStatus(t("connect.status.lost"), "bad");
      endSession(true);
    }
  }
}

async function handleRemoteIce(msg) {
  // The agent gathers ICE non-trickle (candidates ride in its offer), so this is
  // mostly defensive, but honor any trickled candidate it does send.
  if (!pc || !msg.candidate) return;
  try {
    await pc.addIceCandidate(msg.candidate);
  } catch {
    /* ignore a candidate we can't add */
  }
}

// First monitor whose <video> has no stream yet — a fallback when a track's mid
// isn't in the offer map (keeps a single-monitor session working regardless).
function firstUnassignedMonitor() {
  for (const m of monitors) {
    const s = screens.get(m.i);
    if (s && !s.el.srcObject) return m.i;
  }
  return monitors.length ? monitors[0].i : 0;
}

function closePeer() {
  if (mediaFailTimer) {
    clearTimeout(mediaFailTimer);
    mediaFailTimer = null;
  }
  if (pc) {
    try {
      pc.ontrack = null;
      pc.onicecandidate = null;
      pc.onconnectionstatechange = null;
    } catch {
      /* ignore */
    }
    try {
      pc.close();
    } catch {
      /* ignore */
    }
    pc = null;
  }
  trackMids = {};
}

// ------------- Connection heartbeat + interruption / reconnection -------------
// Each tick pings the server over the WS; the server echoes "pong". If no pong
// arrives for INTERRUPT_MS (or the WS drops, or the agent's own link drops), we
// warn the user (Wait/Cut) and actively reconnect while they wait.

function startHeartbeat() {
  lastPong = performance.now();
  lastTick = performance.now();
  if (hbTimer) clearInterval(hbTimer);
  hbTimer = setInterval(heartbeatTick, 1000);
}
function stopHeartbeat() {
  if (hbTimer) {
    clearInterval(hbTimer);
    hbTimer = null;
  }
}

// Our link to the server is unusable right now (closed, or no pong in time, or the
// agent's side dropped). A CONNECTING socket is neither down nor up yet — we hold.
function isDown() {
  if (agentDown) return true;
  if (!ws) return true;
  if (ws.readyState === 0) return false; // opening: onclose/onerror catches failures
  if (ws.readyState !== 1) return true; // closing / closed
  return performance.now() - lastPong > INTERRUPT_MS;
}
// Our link is confirmed healthy again (used to detect recovery).
function isUp() {
  if (agentDown) return false;
  return !!ws && ws.readyState === 1 && performance.now() - lastPong <= INTERRUPT_MS;
}

function heartbeatTick() {
  if (!live) return;
  const now = performance.now();
  // If far more than the 1s interval elapsed, this timer was suspended (backgrounded
  // tab, device sleep, long main-thread stall). lastPong is stale only because JS
  // wasn't running, not because the link dropped — refresh the window and re-ping
  // rather than firing a false interruption (a truly dead socket is still caught:
  // readyState in isDown(), or no pong next cycle).
  const suspended = now - lastTick > INTERRUPT_MS;
  lastTick = now;
  if (suspended && !interruptionActive && ws && ws.readyState === 1) {
    lastPong = now;
    try {
      ws.send("ping");
    } catch {
      /* ignore */
    }
    return;
  }
  if (ws && ws.readyState === 1) {
    try {
      ws.send("ping");
    } catch {
      /* ignore */
    }
  }
  if (!interruptionActive) {
    if (isDown()) onInterruption();
  } else if (isUp()) {
    onRecovered();
  } else if (now - interruptStart > MAX_RECONNECT_MS) {
    // Reconnection never succeeded within the server's grace window: end cleanly so
    // the viewer isn't stuck re-asking forever on a permanently-dead session.
    interruptionActive = false; // endSession() dismisses the dialog + clears timers
    endSession(true);
    setStatus(t("connect.status.disconnected"), "bad");
  }
}

// Interruption begins: start reconnecting IMMEDIATELY, and show the warning only if
// we're still down after WARN_DELAY_MS — so a quick reconnect is invisible.
function onInterruption() {
  if (interruptionActive || !live) return;
  interruptionActive = true;
  interruptStart = performance.now();
  setStatus(t("ui.interrupt.status"), "bad");
  startReconnecting();
  if (dialogTimer) clearTimeout(dialogTimer);
  dialogTimer = setTimeout(showInterruptDialog, WARN_DELAY_MS);
}

function showInterruptDialog() {
  dialogTimer = null;
  if (!interruptionActive || !live) return;
  if (modalOpen) {
    // Another modal is up (e.g. the Disconnect confirmation). We can't stack dialogs,
    // so retry shortly: once it closes and we're still interrupted, the Wait/Cut
    // warning appears rather than being lost for this whole interruption.
    if (reaskTimer) clearTimeout(reaskTimer);
    reaskTimer = setTimeout(showInterruptDialog, PING_MS);
    return;
  }
  confirmDialog(
    t("ui.interrupt.msg"),
    t("ui.interrupt.cut"),
    t("ui.interrupt.wait"),
  ).then((cut) => {
    if (!interruptionActive || !live) return; // recovered (auto-dismissed) or ended
    if (cut === true) {
      // Cut: end the session for good.
      interruptionActive = false;
      stopReconnecting();
      endSession(true);
      setStatus(t("connect.status.disconnectedAgain"), "warn");
      return;
    }
    // "Keep waiting": re-ask after REASK_MS if still interrupted.
    if (reaskTimer) clearTimeout(reaskTimer);
    reaskTimer = setTimeout(showInterruptDialog, REASK_MS);
  });
}

function onRecovered() {
  interruptionActive = false;
  stopReconnecting();
  if (dialogTimer) {
    clearTimeout(dialogTimer);
    dialogTimer = null;
  }
  if (reaskTimer) {
    clearTimeout(reaskTimer);
    reaskTimer = null;
  }
  if (mediaFailTimer) {
    // A media-fail timer armed against the OLD (failed) pc must not fire after the
    // WS link has recovered and end the session we just got back.
    clearTimeout(mediaFailTimer);
    mediaFailTimer = null;
  }
  if (dismissModal) dismissModal(); // close the warning dialog if it's open
  setStatus(
    inSession ? t("connect.status.connected") : t("connect.status.waitingAccept"),
    inSession ? "good" : "warn",
  );
}

function startReconnecting() {
  if (reconnecting) return;
  reconnecting = true;
  tryReconnect();
}
function stopReconnecting() {
  reconnecting = false;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}
function tryReconnect() {
  if (!reconnecting || !interruptionActive || !live) return;
  const ourSocketOpen = !!ws && ws.readyState === 1;
  // Reopen OUR WS whenever our own socket is down (even if agentDown — our link may
  // have dropped too, which would otherwise strand us). If ONLY the agent dropped
  // and our socket is fine, just wait for it to reconnect and re-serve (reopening
  // would hit the DO's "busy" check).
  if (!agentDown || !ourSocketOpen) {
    if (ws && ws.readyState === 1) {
      // Stall on a still-"open" socket: close it first, else the DO sees two viewers.
      intentionalClose = true;
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      ws = null;
    }
    if ((!ws || ws.readyState > 1) && currentCode) openWs(currentCode);
  }
  reconnectTimer = setTimeout(tryReconnect, PING_MS);
}

// A terminal failure while establishing (no-agent / busy): not an interruption —
// stop cleanly so the later WS close doesn't pop the reconnect dialog.
function abortConnect() {
  live = false;
  interruptionActive = false;
  stopHeartbeat();
  stopReconnecting();
  intentionalClose = true;
  if (ws) {
    try {
      ws.close();
    } catch {
      /* ignore */
    }
    ws = null;
  }
}

// ---------------- Session lifecycle ----------------

function startSession(mons) {
  monitors = mons.map((m) => ({ i: m.i, w: m.w, h: m.h }));
  inSession = true;
  blockRects = []; // fresh session: clear any stale agent-window regions

  buildScreens();

  // Default view: grid when there is more than one monitor, else single.
  viewMode = monitors.length > 1 ? "grid" : "single";
  selectedIndex = monitors.length ? monitors[0].i : 0;

  joinEl.hidden = true;
  stageEl.hidden = false;
  fsBtn.hidden = false;
  disconnectBtn.hidden = false;
  placeStatusInBar(); // connected: status returns to the top-left bar

  buildMonitorPicker();
  applyView();
  bindWindowListeners();

  setStatus(t("connect.status.connected"), "good");
  const first = screens.get(selectedIndex);
  if (first) first.el.focus();

  sendWant();
}

function endSession(keepStatus) {
  inSession = false;
  live = false;
  interruptionActive = false;
  agentDown = false;
  stopHeartbeat();
  stopReconnecting();
  if (dialogTimer) {
    clearTimeout(dialogTimer);
    dialogTimer = null;
  }
  if (reaskTimer) {
    clearTimeout(reaskTimer);
    reaskTimer = null;
  }
  blockRects = [];
  if (dismissModal) dismissModal(); // force-close a confirmation modal if one is open
  closePeer();
  if (fsActive) exitFsBar();
  if (document.fullscreenElement) {
    try {
      document.exitFullscreen();
    } catch {
      /* ignore */
    }
  }
  unbindWindowListeners();
  clearScreens();
  if (ws) {
    intentionalClose = true;
    try {
      ws.close();
    } catch {
      /* ignore */
    }
    ws = null;
  }
  stageEl.hidden = true;
  joinEl.hidden = false;
  fsBtn.hidden = true;
  disconnectBtn.hidden = true;
  placeStatusInJoin(); // back on the join screen: status below the center card
  viewControls.hidden = true;
  monitorPicker.hidden = true;
  monitorPicker.innerHTML = "";
  monitors = [];
  viewMode = "grid";
  selectedIndex = 0;
  if (!keepStatus) setStatus("Enter a session code");
}

// The WebSocket closed. If we closed it on purpose (reconnect / endSession / an
// abort), ignore. Otherwise it's an interruption: warn + reconnect, don't end.
function onWsClosed(ev) {
  if (intentionalClose || !live) return;
  // The DO's grace alarm closes sockets with code 1001 ("session expired") once the
  // agent never returns — terminal, so end cleanly rather than reconnecting forever.
  if (ev && ev.code === 1001) {
    interruptionActive = false;
    endSession(true);
    setStatus(t("connect.status.disconnected"), "bad");
    return;
  }
  onInterruption();
}

// ---------------- Screens (one <video> per monitor) ----------------

function buildScreens() {
  clearScreens();
  for (const m of monitors) {
    const el = document.createElement("video");
    el.className = "screen";
    el.autoplay = true;
    el.muted = true;
    el.playsInline = true;
    el.setAttribute("playsinline", ""); // iOS Safari needs the attribute too
    el.tabIndex = 0;
    el.dataset.index = String(m.i);
    const handlers = attachInput(el, m);
    screensEl.appendChild(el);
    screens.set(m.i, { el, handlers, mon: m });
  }
}

function clearScreens() {
  for (const [, s] of screens) {
    const h = s.handlers;
    s.el.removeEventListener("mousemove", h.onMove);
    s.el.removeEventListener("mousedown", h.onDown);
    s.el.removeEventListener("wheel", h.onWheel);
    s.el.removeEventListener("contextmenu", h.onCtx);
    try {
      s.el.srcObject = null;
    } catch {
      /* ignore */
    }
  }
  screens.clear();
  screensEl.innerHTML = "";
  activeDown = null;
}

// ---------------- View modes ----------------

function currentWant() {
  if (viewMode === "grid") return monitors.map((m) => m.i);
  return [selectedIndex];
}

function sendWant() {
  send({ type: "want", monitors: currentWant() });
}

function buildMonitorPicker() {
  monitorPicker.innerHTML = "";
  const multi = monitors.length > 1;
  viewControls.hidden = !multi; // mode buttons only make sense with >1 monitor
  if (!multi) {
    monitorPicker.hidden = true;
    return;
  }
  monitors.forEach((m, n) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn mon-btn";
    b.textContent = String(n + 1);
    b.dataset.index = String(m.i);
    b.title = t("connect.monitor.title")
      .replace("{n}", n + 1)
      .replace("{w}", m.w)
      .replace("{h}", m.h);
    b.addEventListener("click", () => selectMonitor(m.i));
    monitorPicker.appendChild(b);
  });
}

function applyView() {
  screensEl.classList.toggle("grid", viewMode === "grid");
  screensEl.classList.toggle("single", viewMode === "single");

  for (const [idx, s] of screens) {
    s.el.hidden = !(viewMode === "grid" || idx === selectedIndex);
  }

  gridBtn.classList.toggle("active", viewMode === "grid");
  singleBtn.classList.toggle("active", viewMode === "single");

  const multi = monitors.length > 1;
  monitorPicker.hidden = !(multi && viewMode === "single");
  for (const b of monitorPicker.children) {
    b.classList.toggle("active", Number(b.dataset.index) === selectedIndex);
  }
}

function setMode(mode) {
  if (!inSession || viewMode === mode) return;
  viewMode = mode;
  applyView();
  if (mode === "single") {
    const s = screens.get(selectedIndex);
    if (s) s.el.focus();
  }
  sendWant();
}

function selectMonitor(idx) {
  if (!inSession) return;
  const changed = viewMode !== "single" || selectedIndex !== idx;
  viewMode = "single";
  selectedIndex = idx;
  applyView();
  const s = screens.get(idx);
  if (s) s.el.focus();
  if (changed) sendWant();
}

gridBtn.addEventListener("click", () => setMode("grid"));
singleBtn.addEventListener("click", () => setMode("single"));

// ---------------- Fullscreen hover bar ----------------
// In fullscreen the top app bar is off-screen, so the view controls move into a
// bar that slides down when the mouse reaches the top edge. The SAME control
// elements are reused (their handlers travel with them), then moved back on exit.

function moveControlsToFsBar() {
  // Keep the view controls to the left of the Exit-fullscreen button.
  if (viewControls.parentElement !== fsBar) {
    fsBar.insertBefore(viewControls, fsExitBtn);
  }
  viewControls.hidden = false;
}

function restoreControlsToBar() {
  if (viewControls.parentElement !== barActions) {
    barActions.insertBefore(viewControls, kbLabel);
  }
  viewControls.hidden = monitors.length <= 1;
}

function setBarOpen(open) {
  fsBar.classList.toggle("show", open);
  // The hint tab rides with the bar and goes fully opaque while the bar is down.
  stageEl.classList.toggle("bar-open", open);
}

// A press that STARTS anywhere other than the hint (typically a drag on the remote
// screen) locks the hint out until the button is released, so moving the pointer
// over it can't light it up or toggle the menu. A press on the hint itself is a
// normal click.
function onFsPointerDown(e) {
  if (!fsActive) return;
  if (!fsHint.contains(e.target)) stageEl.classList.add("drag-lock");
}
function onFsPointerUp() {
  stageEl.classList.remove("drag-lock");
}

// The menu is deployed ONLY by the arrow hint: clicking it toggles the bar open or
// closed, wherever the arrow currently sits. It never opens on its own anymore.
fsHint.addEventListener("click", () => {
  if (fsActive) setBarOpen(!fsBar.classList.contains("show"));
});

// Keyboard Lock (Chrome/Edge, secure context): while in fullscreen, route Esc to
// the remote computer instead of exiting, and suppress the browser's own exit
// overlay/notification — the browser switches to "press and hold Esc to exit"
// instead of showing a hover X. No-op where unsupported (e.g. Firefox).
function lockEscape() {
  try {
    if (navigator.keyboard && navigator.keyboard.lock) {
      navigator.keyboard.lock(["Escape"]).catch(() => {});
    }
  } catch {
    /* ignore */
  }
}
function unlockEscape() {
  try {
    if (navigator.keyboard && navigator.keyboard.unlock) navigator.keyboard.unlock();
  } catch {
    /* ignore */
  }
}

function enterFsBar() {
  if (fsActive) return;
  fsActive = true;
  lockEscape();
  if (monitors.length > 1) moveControlsToFsBar(); // view controls only if multi
  stageEl.classList.add("fs-on"); // reveal the arrow handle (menu starts closed)
  document.addEventListener("mousedown", onFsPointerDown, true);
  document.addEventListener("mouseup", onFsPointerUp, true);
}

function exitFsBar() {
  if (!fsActive) return;
  fsActive = false;
  unlockEscape();
  document.removeEventListener("mousedown", onFsPointerDown, true);
  document.removeEventListener("mouseup", onFsPointerUp, true);
  fsBar.classList.remove("show");
  stageEl.classList.remove("fs-on", "bar-open", "drag-lock");
  restoreControlsToBar();
}

document.addEventListener("fullscreenchange", () => {
  const fs = document.fullscreenElement === stageEl;
  // The hover bar appears in any fullscreen session (it always has the Exit
  // button); the Grid/Single/monitor controls are added only when multi-monitor.
  if (fs && inSession) enterFsBar();
  else exitFsBar();
});

// ---------------- Input ----------------

// Map an event to the target monitor's FULL pixel coordinate space (mon.w/mon.h
// from the "monitors" message), independent of the encoded video resolution.
function mediaXY(el, mon, e) {
  const r = el.getBoundingClientRect();
  const x = r.width ? ((e.clientX - r.left) / r.width) * mon.w : 0;
  const y = r.height ? ((e.clientY - r.top) / r.height) * mon.h : 0;
  return {
    x: Math.max(0, Math.min(mon.w - 1, Math.round(x))),
    y: Math.max(0, Math.min(mon.h - 1, Math.round(y))),
  };
}

function attachInput(el, mon) {
  let lastMove = 0;

  const onMove = (e) => {
    const p = mediaXY(el, mon, e);
    // Over the agent's own window the agent ignores clicks, so hint with a
    // "not-allowed" cursor (updated every move; "" reverts to the CSS crosshair).
    el.style.cursor = inBlockRect(mon.i, p.x, p.y) ? "not-allowed" : "";
    const now = performance.now();
    if (now - lastMove < 25) return; // ~40 moves/sec max
    lastMove = now;
    send({ t: "mm", m: mon.i, x: p.x, y: p.y });
  };
  const onDown = (e) => {
    e.preventDefault();
    el.focus();
    activeDown = { el, mon };
    const p = mediaXY(el, mon, e);
    send({ t: "md", m: mon.i, x: p.x, y: p.y, b: e.button });
  };
  const onWheel = (e) => {
    e.preventDefault();
    const p = mediaXY(el, mon, e);
    send({ t: "scroll", m: mon.i, x: p.x, y: p.y, dx: e.deltaX, dy: e.deltaY });
  };
  const onCtx = (e) => {
    e.preventDefault(); // let right-click flow through as md/mu instead
  };

  el.addEventListener("mousemove", onMove);
  el.addEventListener("mousedown", onDown);
  el.addEventListener("wheel", onWheel, { passive: false });
  el.addEventListener("contextmenu", onCtx);
  return { onMove, onDown, onWheel, onCtx };
}

// A single window-level mouseup pairs every mousedown with exactly one mouseup
// on the monitor where the press began, even when the button is released off
// the video (a grid gap, outside the stage, another monitor), so the remote
// button never gets stuck down.
function onWindowMouseUp(e) {
  if (!activeDown) return;
  const { el, mon } = activeDown;
  activeDown = null;
  const p = mediaXY(el, mon, e);
  send({ t: "mu", m: mon.i, x: p.x, y: p.y, b: e.button });
}

function onKeyDown(e) {
  if (modalOpen || !inSession || !kbToggle.checked) return;
  e.preventDefault();
  send({ t: "kd", key: e.key, code: e.code });
}
function onKeyUp(e) {
  if (modalOpen || !inSession || !kbToggle.checked) return;
  e.preventDefault();
  send({ t: "ku", key: e.key, code: e.code });
}

function bindWindowListeners() {
  if (windowBound) return;
  windowBound = true;
  window.addEventListener("mouseup", onWindowMouseUp);
  window.addEventListener("keydown", onKeyDown, true);
  window.addEventListener("keyup", onKeyUp, true);
}

function unbindWindowListeners() {
  if (!windowBound) return;
  windowBound = false;
  window.removeEventListener("mouseup", onWindowMouseUp);
  window.removeEventListener("keydown", onKeyDown, true);
  window.removeEventListener("keyup", onKeyUp, true);
}

// ---------------- Controls ----------------

// A themed confirmation modal. Resolves true (confirm) or false (cancel/Escape/
// backdrop). While it is open, remote keyboard input is suppressed (modalOpen).
function confirmDialog(message, okText, cancelText, { danger = true } = {}) {
  if (modalOpen) return Promise.resolve(false); // never stack confirmation dialogs
  return new Promise((resolve) => {
    modalOpen = true;
    const overlay = document.createElement("div");
    overlay.className = "modal";
    const card = document.createElement("div");
    card.className = "modal-card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    const msg = document.createElement("p");
    msg.className = "modal-msg";
    msg.textContent = message;
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "btn";
    cancelBtn.textContent = cancelText;
    const okBtn = document.createElement("button");
    okBtn.className = "btn " + (danger ? "danger" : "primary");
    okBtn.textContent = okText;
    actions.append(cancelBtn, okBtn);
    card.append(msg, actions);
    overlay.appendChild(card);
    // In fullscreen only the fullscreen element's subtree renders, so the modal
    // must live inside it; otherwise document.body.
    (document.fullscreenElement || document.body).appendChild(overlay);
    void overlay.offsetWidth; // force reflow so the fade-in transition always runs
    overlay.classList.add("show");

    let closed = false;
    function close(result) {
      if (closed) return;
      closed = true;
      modalOpen = false;
      dismissModal = null;
      overlay.classList.remove("show");
      setTimeout(() => overlay.remove(), 200);
      document.removeEventListener("keydown", onKey, true);
      resolve(result);
    }
    dismissModal = () => close(false); // let endSession() dismiss this from outside
    function onKey(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        close(false);
      } else if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
        close(true);
      }
    }
    cancelBtn.addEventListener("click", () => close(false));
    okBtn.addEventListener("click", () => close(true));
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close(false);
    });
    document.addEventListener("keydown", onKey, true);
    okBtn.focus();
  });
}

// "Share this computer" always opens the Share page in a NEW tab (target="_blank"
// in the markup), so it never interrupts the current connection.

// Shared by the top-bar Disconnect and the in-fullscreen Disconnect button.
async function requestDisconnect() {
  const ok = await confirmDialog(
    t("connect.confirm.disconnect"),
    t("connect.btn.disconnect"),
    t("ui.cancel"),
  );
  // The session may have ended on its own while the dialog was up.
  if (!ok || !inSession) return;
  endSession(true); // also exits fullscreen
  setStatus(t("connect.status.disconnectedAgain"), "warn");
}
disconnectBtn.addEventListener("click", requestDisconnect);
fsDisconnectBtn.addEventListener("click", requestDisconnect);

fsBtn.addEventListener("click", () => {
  if (stageEl.requestFullscreen) stageEl.requestFullscreen();
});

fsExitBtn.addEventListener("click", () => {
  if (document.fullscreenElement) {
    try {
      document.exitFullscreen();
    } catch {
      /* ignore */
    }
  }
});

// Warn before leaving (closing the tab/window or navigating away) while a
// connection is live. The browser shows its native "Leave site?" confirmation;
// confirming unloads the page, which closes the WebSocket and ends the session.
window.addEventListener("beforeunload", (e) => {
  if (ws) {
    e.preventDefault();
    e.returnValue = ""; // required by some browsers to trigger the prompt
  }
});
