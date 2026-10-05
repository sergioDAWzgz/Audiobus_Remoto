// Audiobus Remote — browser controller (viewer side).
// Protocol v2 (multi-monitor): the agent sends a one-time "monitors" message,
// then one BINARY WebSocket message per captured frame where byte 0 is the
// 0-based monitor index and the remaining bytes are that monitor's JPEG. The
// viewer tells the agent which monitors to stream with a "want" message, and
// sends input tagged with the target monitor index "m".

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
let screens = new Map(); // monitor index -> { canvas, ctx, handlers, mon }
let inSession = false;
let viewMode = "grid"; // "grid" | "single"
let selectedIndex = 0; // monitor index shown in single mode
let activeDown = null; // { canvas, mon } held between mousedown and mouseup
let windowBound = false;
let fsActive = false; // the fullscreen hover-bar is live
let modalOpen = false; // a confirmation dialog is open (suppresses remote input)
let dismissModal = null; // close() of the open confirmation dialog, or null
let lastFrameAt = 0; // performance.now() of the last frame/activity
let watchdog = null; // interval that detects a dead/stopped agent
const STALE_MS = 4000; // no frames for this long -> treat the session as gone

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
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/viewer?code=${code}`);
  ws.binaryType = "arraybuffer";
  setStatus(t("connect.status.connecting"));
  ws.onopen = () => setStatus(t("connect.status.waitingAccept"), "warn");
  ws.onmessage = onMessage;
  ws.onclose = onClose;
  ws.onerror = () => {};
}

function send(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}

// ---------------- Messages ----------------

async function onMessage(ev) {
  if (typeof ev.data !== "string") {
    await drawFrame(ev.data);
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
      startSession(Array.isArray(msg.monitors) ? msg.monitors : []);
      break;
    case "denied":
      setStatus(t("connect.status.declined"), "bad");
      endSession(true);
      break;
    case "error": {
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

async function drawFrame(buf) {
  lastFrameAt = performance.now(); // liveness: a frame just arrived
  try {
    const bytes = new Uint8Array(buf);
    if (bytes.length < 2) return; // need an index byte plus at least some JPEG
    const idx = bytes[0];
    const s = screens.get(idx);
    if (!s) return; // frame for a monitor we have no canvas for — ignore
    const bmp = await createImageBitmap(new Blob([buf.slice(1)]));
    s.ctx.drawImage(bmp, 0, 0, s.canvas.width, s.canvas.height);
    bmp.close();
  } catch {
    /* skip a bad frame */
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
  if (first) first.canvas.focus();

  // Watchdog: the agent streams continuously while connected, so a gap in frames
  // means it stopped/crashed/dropped. This catches cases where no explicit
  // disconnect ever arrives (half-open socket), including while in fullscreen.
  lastFrameAt = performance.now();
  if (watchdog) clearInterval(watchdog);
  watchdog = setInterval(checkStale, 1000);

  sendWant();
}

function checkStale() {
  if (!inSession) return;
  if (performance.now() - lastFrameAt > STALE_MS) {
    setStatus(t("connect.status.lost"), "bad");
    endSession(true);
  }
}

function endSession(keepStatus) {
  inSession = false;
  blockRects = [];
  if (dismissModal) dismissModal(); // force-close a confirmation modal if one is open
  if (watchdog) {
    clearInterval(watchdog);
    watchdog = null;
  }
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

function onClose() {
  if (inSession) setStatus(t("connect.status.disconnected"), "bad");
  endSession(true);
}

// ---------------- Canvases ----------------

function buildScreens() {
  clearScreens();
  for (const m of monitors) {
    const canvas = document.createElement("canvas");
    canvas.className = "screen";
    canvas.width = m.w; // intrinsic pixel size of this monitor
    canvas.height = m.h;
    canvas.tabIndex = 0;
    canvas.dataset.index = String(m.i);
    const ctx = canvas.getContext("2d", { alpha: false });
    const handlers = attachInput(canvas, m);
    screensEl.appendChild(canvas);
    screens.set(m.i, { canvas, ctx, handlers, mon: m });
  }
}

function clearScreens() {
  for (const [, s] of screens) {
    const h = s.handlers;
    s.canvas.removeEventListener("mousemove", h.onMove);
    s.canvas.removeEventListener("mousedown", h.onDown);
    s.canvas.removeEventListener("wheel", h.onWheel);
    s.canvas.removeEventListener("contextmenu", h.onCtx);
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
  lastFrameAt = performance.now(); // grace while the agent switches monitors
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
    s.canvas.hidden = !(viewMode === "grid" || idx === selectedIndex);
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
    if (s) s.canvas.focus();
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
  if (s) s.canvas.focus();
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

// Map an event to this canvas's own pixel coordinate space.
function canvasXY(canvas, e) {
  const r = canvas.getBoundingClientRect();
  const x = r.width ? ((e.clientX - r.left) / r.width) * canvas.width : 0;
  const y = r.height ? ((e.clientY - r.top) / r.height) * canvas.height : 0;
  return {
    x: Math.max(0, Math.min(canvas.width - 1, Math.round(x))),
    y: Math.max(0, Math.min(canvas.height - 1, Math.round(y))),
  };
}

function attachInput(canvas, mon) {
  let lastMove = 0;

  const onMove = (e) => {
    const p = canvasXY(canvas, e);
    // Over the agent's own window the agent ignores clicks, so hint with a
    // "not-allowed" cursor (updated every move; "" reverts to the CSS crosshair).
    canvas.style.cursor = inBlockRect(mon.i, p.x, p.y) ? "not-allowed" : "";
    const now = performance.now();
    if (now - lastMove < 25) return; // ~40 moves/sec max
    lastMove = now;
    send({ t: "mm", m: mon.i, x: p.x, y: p.y });
  };
  const onDown = (e) => {
    e.preventDefault();
    canvas.focus();
    activeDown = { canvas, mon };
    const p = canvasXY(canvas, e);
    send({ t: "md", m: mon.i, x: p.x, y: p.y, b: e.button });
  };
  const onWheel = (e) => {
    e.preventDefault();
    const p = canvasXY(canvas, e);
    send({ t: "scroll", m: mon.i, x: p.x, y: p.y, dx: e.deltaX, dy: e.deltaY });
  };
  const onCtx = (e) => {
    e.preventDefault(); // let right-click flow through as md/mu instead
  };

  canvas.addEventListener("mousemove", onMove);
  canvas.addEventListener("mousedown", onDown);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("contextmenu", onCtx);
  return { onMove, onDown, onWheel, onCtx };
}

// A single window-level mouseup pairs every mousedown with exactly one mouseup
// on the monitor where the press began, even when the button is released off
// the canvas (a grid gap, outside the stage, another monitor), so the remote
// button never gets stuck down.
function onWindowMouseUp(e) {
  if (!activeDown) return;
  const { canvas, mon } = activeDown;
  activeDown = null;
  const p = canvasXY(canvas, e);
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
