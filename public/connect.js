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
const kbToggle = $("kbToggle"); // the keyboard-forwarding switch (a styled checkbox)
const kbSwitch = $("kbSwitch"); // its label wrapper (anchor for inserting viewControls)
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
const sendFileBtn = $("sendFileBtn");
const fsSendFileBtn = $("fsSendFileBtn");
const fileInput = $("fileInput");
const dropHint = $("dropHint");
const transfersEl = $("transfers");
const barActions = document.querySelector(".bar-actions");
const barLeft = document.querySelector(".bar-left");
const topBar = document.querySelector(".bar");
const joinStatusSlot = $("joinStatusSlot");
const joinCard = $("joinCard");
const pinCard = $("pinCard");
const pinForm = $("pinForm");
const pinInput = $("pinInput");
const pinError = $("pinError");
const pinDesc = $("pinDesc");

// Translation helper (i18n.js loads first; falls back to the key if missing).
const t = (k) => (window.I18N ? window.I18N.t(k) : k);

let ws = null;
let monitors = []; // [{ i, w, h }, ...] in the order the agent sent them
let screens = new Map(); // monitor index -> { el, handlers, mon }
let inSession = false;
let pinActive = false; // showing the PIN gate (host auto-accepts behind a PIN)
let pinLen = 4; // digits expected, per the agent's pin-required message
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
const PING_MS = 1000; // send a heartbeat ping this often
const INTERRUPT_MS = 3000; // no pong for this long -> the server link is interrupted
const WARN_DELAY_MS = 2000; // reconnect starts immediately; show the Wait/Cut dialog this
//                             long after the drop is detected -> ~5s total (3s + 2s)
const STALE_MS = 3000; // video not advancing for this long (agent-side drop) -> interrupted
const REASK_MS = 10000; // after "Wait", re-ask this long later if still interrupted
const MAX_RECONNECT_MS = 65000; // > the DO's agent grace (60s): give up after this, end cleanly
let live = false; // true from connect() until endSession()
let lastPong = 0; // performance.now() of the last "pong" from the server
let lastTick = 0; // performance.now() of the previous heartbeat tick (to spot suspensions)
let lastVideoAdvance = 0; // performance.now() when any monitor's <video> last advanced
let videoEverPlayed = false; // a frame has actually played this session (so a stall is a
//                              real stop, not just slow initial WebRTC setup)
let hbTimer = null; // heartbeat interval id
let agentDown = false; // the AGENT's link to the server dropped (relayed by the DO)
let interruptionActive = false; // an interruption (reconnecting; maybe dialog) is in progress
let interruptStart = 0; // performance.now() when the current interruption began
let dialogTimer = null; // delay before the warning dialog first appears
let reaskTimer = null; // delay before re-asking after "Keep waiting"
let mediaFailTimer = null; // confirms a sustained media (WebRTC) failure before acting
let mediaDown = false; // the WebRTC media path has failed (agent likely dropped) — folded
//                        into isDown()/isUp() so it drives the warn+reconnect flow, not an end
let reconnecting = false; // actively trying to reopen our WS
let reconnectTimer = null;
let intentionalClose = false; // we closed the WS on purpose (reconnect / endSession)
// This browser's stable RESUME TOKEN for the current session. Minted fresh for each
// user-initiated join and reused on every reconnect of that session (its own-link drop
// or a dual-drop), so the agent recognises us as the SAME approved viewer and resumes
// WITHOUT a new consent prompt. A fresh join (or another browser) mints a different,
// unguessable token, so a different person who has the code still has to be approved.
let vtok = null;
function newVtok() {
  try {
    if (crypto && crypto.randomUUID) return crypto.randomUUID();
  } catch {
    /* fall through */
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

// ---- Clipboard sync (bidirectional, best-effort) ----
// The agent streams its clipboard changes to us (we writeText them); we send ours to the
// agent. Browsers give NO clipboard-change event and readText() needs focus + the
// clipboard-read permission, so we sample the local clipboard when the remote view regains
// focus/visibility and on pointer interaction (a user gesture, which lets Chrome show the
// one-time permission prompt). clipboard-write is granted by default to focused secure
// contexts, so inbound host->viewer works without a prompt. Firefox has no readText(), so
// viewer->host sync is Chromium/Safari only (host->viewer still works) — consistent with
// the app already relying on Chromium-only Keyboard Lock.
const CLIP_MAX_BYTES = 256 * 1024; // cap synced clipboard text; must match the agent
const CLIP_IMG_MAX_BYTES = 16 * 1024 * 1024; // cap synced clipboard image; matches the agent
let clipCaps = false; // the agent advertised clipboard support (caps.clip)
let clipLast = null; // last clipboard TEXT reconciled with the host (sent OR received)
let clipLastImg = null; // hash of the last clipboard IMAGE reconciled (echo guard)
let clipReadBusy = false; // a clipboard read is in flight (avoid overlap)
let clipReadAt = 0; // performance.now() of the last read attempt (throttle)
let clipImgSeq = 0; // local counter for outbound clipboard-image ids
let clipImgRx = null; // in-flight incoming clipboard image: { id, parts, got }

function clipBytes(s) {
  try {
    return new TextEncoder().encode(s).length;
  } catch {
    return s.length;
  }
}

// Fast sampled content hash (FNV-1a over up to ~4096 sampled bytes + length), for
// echo-guarding clipboard images without re-sending our own writes.
function hashBytes(u8) {
  let h = 0x811c9dc5;
  const step = Math.max(1, Math.floor(u8.length / 4096));
  for (let i = 0; i < u8.length; i += step) {
    h ^= u8[i];
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0) + ":" + u8.length;
}

// Send while pacing on the socket's buffered amount, so a big clipboard image can't bloat
// the WS send buffer and starve the heartbeat ping (the agent paces via await ws.send).
async function sendPaced(obj) {
  let waited = 0;
  while (ws && ws.readyState === 1 && ws.bufferedAmount > 1000000 && waited < 15000) {
    await new Promise((r) => setTimeout(r, 20));
    waited += 20;
  }
  send(obj);
}

async function sendClipImage(bytes) {
  const id = "ci" + ++clipImgSeq;
  await sendPaced({ type: "clip-img-begin", id, size: bytes.length });
  for (let off = 0, n = 0; off < bytes.length; off += FILE_CHUNK_BYTES, n++) {
    if (!inSession || !clipCaps) return;
    await sendPaced({
      type: "clip-img-chunk",
      id,
      n,
      data: bytesToBase64(bytes.subarray(off, off + FILE_CHUNK_BYTES)),
    });
  }
  send({ type: "clip-img-end", id });
}

async function syncClipboardUp() {
  if (!inSession || !clipCaps || clipReadBusy) return;
  if (!navigator.clipboard) return;
  const now = performance.now();
  if (now - clipReadAt < 400) return; // throttle: fires on every click/focus
  clipReadAt = now;
  clipReadBusy = true;
  try {
    if (navigator.clipboard.read) {
      // Chromium/Safari: read() exposes images AND text.
      const items = await navigator.clipboard.read();
      for (const item of items) {
        const imgType = item.types.find((t) => t.startsWith("image/"));
        if (imgType) {
          const blob = await item.getType(imgType);
          if (blob.size > CLIP_IMG_MAX_BYTES) return; // too big; never materialize it
          const bytes = new Uint8Array(await blob.arrayBuffer());
          const h = hashBytes(bytes);
          if (h !== clipLastImg) {
            clipLastImg = h;
            clipLast = null;
            await sendClipImage(bytes);
          }
          return;
        }
      }
      for (const item of items) {
        if (item.types.includes("text/plain")) {
          const text = await (await item.getType("text/plain")).text();
          if (text !== clipLast) {
            clipLast = text;
            clipLastImg = null;
            if (clipBytes(text) <= CLIP_MAX_BYTES) send({ type: "clip", text });
          }
          return;
        }
      }
    } else if (navigator.clipboard.readText) {
      // Firefox: text only (no read()/image support).
      const text = await navigator.clipboard.readText();
      if (text !== clipLast) {
        clipLast = text;
        clipLastImg = null;
        if (clipBytes(text) <= CLIP_MAX_BYTES) send({ type: "clip", text });
      }
    }
  } catch {
    // No permission/focus yet: rejects quietly (a pointerdown gesture prompts in Chrome).
  } finally {
    clipReadBusy = false;
  }
}

function applyRemoteClipboard(text) {
  clipLast = text; // record first so our own up-sync won't bounce it back to the host
  clipLastImg = null;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => {});
  }
}

// ---- incoming clipboard image (host -> viewer), chunked ----
function onClipImgBegin(msg) {
  if (!inSession || typeof msg.id !== "string") return;
  if ((Number(msg.size) || 0) > CLIP_IMG_MAX_BYTES) return;
  clipImgRx = { id: msg.id, parts: [], got: 0 };
}
function onClipImgChunk(msg) {
  if (!clipImgRx || clipImgRx.id !== msg.id || typeof msg.data !== "string") return;
  let bytes;
  try {
    bytes = base64ToBytes(msg.data);
  } catch {
    clipImgRx = null;
    return;
  }
  if (clipImgRx.got + bytes.length > CLIP_IMG_MAX_BYTES) {
    clipImgRx = null;
    return;
  }
  clipImgRx.parts.push(bytes);
  clipImgRx.got += bytes.length;
}
function onClipImgEnd(msg) {
  if (!clipImgRx || clipImgRx.id !== msg.id) return;
  const parts = clipImgRx.parts;
  clipImgRx = null;
  clipLast = null;
  const blob = new Blob(parts, { type: "image/png" });
  // Provisional echo-guard from the wire bytes (used if the reseed read below can't run).
  blob.arrayBuffer().then((ab) => {
    clipLastImg = hashBytes(new Uint8Array(ab));
  });
  if (navigator.clipboard && navigator.clipboard.write && window.ClipboardItem) {
    navigator.clipboard
      .write([new ClipboardItem({ "image/png": blob })])
      .then(reseedClipImgGuard) // browsers RE-ENCODE images on write, so seed the guard
      .catch(() => {}); //         from the bytes read() will actually return next
  }
}

// After writing a received image, record the hash the browser will hand back on the next
// read() — it re-encodes PNGs, so hashing the bytes we wrote would mis-detect our own write
// as a local change and bounce it back to the host. Best-effort: if the read can't run
// (no focus/permission), the provisional wire-byte hash stands (bounded to one echo).
async function reseedClipImgGuard() {
  try {
    if (!navigator.clipboard.read) return;
    const items = await navigator.clipboard.read();
    for (const item of items) {
      const t = item.types.find((x) => x.startsWith("image/"));
      if (t) {
        clipLastImg = hashBytes(new Uint8Array(await (await item.getType(t)).arrayBuffer()));
        return;
      }
    }
  } catch {
    /* focus/permission unavailable; provisional hash stands */
  }
}

function onClipFocus() {
  syncClipboardUp();
}
function onClipVisible() {
  if (document.visibilityState === "visible") syncClipboardUp();
}
function onClipPointerDown() {
  syncClipboardUp();
}

// ---- File transfer (both directions, base64 chunks over the relay) ----
// Must mirror the agent's constants. Files are chunked and windowed (at most FILE_WINDOW
// chunks unacked) so a slow peer can't make us flood the relay and starve the heartbeat.
const FILE_CHUNK_BYTES = 128 * 1024;
const FILE_MAX_BYTES = 2 * 1024 * 1024 * 1024;
const FILE_WINDOW = 8;
let fileCaps = false; // the agent advertised file-transfer support (caps.file)
let xferSeq = 0; // local counter for unique upload ids
const uploads = new Map(); // id -> { name, size, sent, n, acked, cancel, wake, done, failed, dir }
const downloads = new Map(); // id -> { name, size, got, parts, mime, done, failed, dir }
// Uploads run ONE AT A TIME through this queue. Each upload's window already bounds its
// own in-flight chunks; serializing keeps the GLOBAL in-flight bytes bounded too, so a
// multi-file drop can't burst the WebSocket send buffer and starve the heartbeat ping.
const uploadQueue = [];
let uploadRunning = false;

function bytesToBase64(bytes) {
  let bin = "";
  const CH = 0x8000; // chunk the String.fromCharCode spread to dodge arg-count limits
  for (let i = 0; i < bytes.length; i += CH) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  }
  return btoa(bin);
}
function base64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function xferPct(st) {
  const total = st.size || 0;
  const cur = st.dir === "up" ? st.sent : st.got;
  if (!total) return st.done ? 100 : 0;
  return Math.min(100, Math.floor((cur / total) * 100));
}

function renderTransfers() {
  if (!transfersEl) return;
  const items = [];
  uploads.forEach((st, id) => items.push([id, st]));
  downloads.forEach((st, id) => items.push([id, st]));
  if (!items.length) {
    transfersEl.hidden = true;
    transfersEl.innerHTML = "";
    return;
  }
  transfersEl.hidden = false;
  transfersEl.innerHTML = "";
  for (const [, st] of items) {
    const pct = xferPct(st);
    const row = document.createElement("div");
    row.className = "xfer" + (st.failed ? " failed" : st.done ? " done" : "");
    row.innerHTML =
      '<div class="xfer-top"><span class="xfer-name"></span>' +
      '<span class="xfer-pct"></span></div>' +
      '<div class="xfer-bar"><div class="xfer-fill"></div></div>';
    row.querySelector(".xfer-name").textContent =
      (st.dir === "up" ? "↑ " : "↓ ") + st.name; // textContent: no filename XSS
    row.querySelector(".xfer-pct").textContent = st.failed
      ? t("connect.file.failed")
      : pct + "%";
    row.querySelector(".xfer-fill").style.width = pct + "%";
    transfersEl.appendChild(row);
  }
}

function finishTransfer(id, map, failed) {
  const st = map.get(id);
  if (!st) return;
  st.done = true;
  st.failed = !!failed;
  renderTransfers();
  // Let the finished row linger briefly, then drop it.
  setTimeout(() => {
    map.delete(id);
    renderTransfers();
  }, 4000);
}

function enqueueUpload(file) {
  if (!inSession || !fileCaps || !file) return;
  uploadQueue.push(file);
  pumpUploads();
}

async function pumpUploads() {
  if (uploadRunning) return;
  uploadRunning = true;
  try {
    while (uploadQueue.length) {
      if (!inSession || !fileCaps) {
        uploadQueue.length = 0;
        break;
      }
      await uploadFile(uploadQueue.shift());
    }
  } finally {
    uploadRunning = false;
  }
}

// viewer -> host: stream a dropped/picked File to the agent.
async function uploadFile(file) {
  if (!inSession || !fileCaps || !file) return;
  const id = "v" + ++xferSeq + "-" + Math.random().toString(36).slice(2);
  const st = {
    name: file.name || "file",
    size: file.size,
    sent: 0,
    n: 0,
    acked: -1,
    cancel: false,
    wake: null,
    done: false,
    failed: false,
    dir: "up",
  };
  uploads.set(id, st);
  renderTransfers();
  if (file.size > FILE_MAX_BYTES) {
    finishTransfer(id, uploads, true);
    return;
  }
  send({
    type: "file-begin",
    id,
    name: st.name,
    size: file.size,
    mime: file.type || "",
  });
  try {
    let offset = 0;
    while (offset < file.size && !st.cancel) {
      // Flow control: wait while FILE_WINDOW chunks are still unacked.
      while (!st.cancel && st.n - st.acked > FILE_WINDOW) {
        await new Promise((res) => {
          st.wake = res;
        });
      }
      if (st.cancel) break;
      if (!ws || ws.readyState !== 1) {
        st.cancel = true;
        break;
      }
      const slice = file.slice(offset, offset + FILE_CHUNK_BYTES);
      const buf = new Uint8Array(await slice.arrayBuffer());
      if (st.cancel) break;
      send({ type: "file-chunk", id, n: st.n, data: bytesToBase64(buf) });
      offset += buf.length;
      st.sent = offset;
      st.n += 1;
      renderTransfers();
    }
    if (st.cancel) {
      send({ type: "file-cancel", id });
      finishTransfer(id, uploads, true);
    } else {
      send({ type: "file-end", id });
      finishTransfer(id, uploads, false);
    }
  } catch {
    st.cancel = true;
    send({ type: "file-cancel", id });
    finishTransfer(id, uploads, true);
  }
}

// host -> viewer: accept an incoming file and save it when complete.
function onFileBegin(msg) {
  if (!inSession || typeof msg.id !== "string" || downloads.has(msg.id)) return;
  const size = Number(msg.size) || 0;
  if (size > FILE_MAX_BYTES) {
    send({ type: "file-cancel", id: msg.id });
    return;
  }
  downloads.set(msg.id, {
    name: (msg.name || "file").toString().slice(0, 200),
    size,
    got: 0,
    parts: [],
    mime: typeof msg.mime === "string" ? msg.mime : "",
    done: false,
    failed: false,
    dir: "down",
  });
  renderTransfers();
}

function onFileChunk(msg) {
  const dl = downloads.get(msg.id);
  if (!dl || typeof msg.data !== "string") return;
  let bytes;
  try {
    bytes = base64ToBytes(msg.data);
  } catch {
    send({ type: "file-cancel", id: msg.id });
    finishTransfer(msg.id, downloads, true);
    return;
  }
  dl.parts.push(bytes);
  dl.got += bytes.length;
  send({ type: "file-ack", id: msg.id, n: msg.n });
  renderTransfers();
}

function onFileEnd(msg) {
  const dl = downloads.get(msg.id);
  if (!dl) return;
  const blob = new Blob(dl.parts, dl.mime ? { type: dl.mime } : undefined);
  dl.parts = []; // release the chunk buffers; the Blob owns the data now
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = dl.name || "file";
  // In fullscreen only the #stage subtree renders, so anchor the click there.
  (document.fullscreenElement || document.body).appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 15000);
  finishTransfer(msg.id, downloads, false);
}

function onFileCancel(msg) {
  const up = uploads.get(msg.id);
  if (up) {
    up.cancel = true;
    if (up.wake) {
      const w = up.wake;
      up.wake = null;
      w();
    }
    finishTransfer(msg.id, uploads, true);
  }
  if (downloads.has(msg.id)) finishTransfer(msg.id, downloads, true);
}

function onFileAck(msg) {
  const up = uploads.get(msg.id);
  if (!up) return;
  up.acked = Math.max(up.acked, Number(msg.n) || 0);
  if (up.wake) {
    const w = up.wake;
    up.wake = null;
    w(); // free the sender if it was waiting on the window
  }
}

function cancelAllTransfers() {
  uploadQueue.length = 0; // drop anything still waiting to start
  uploads.forEach((st) => {
    st.cancel = true;
    if (st.wake) {
      const w = st.wake;
      st.wake = null;
      w();
    }
  });
  uploads.clear();
  downloads.clear();
  if (transfersEl) {
    transfersEl.hidden = true;
    transfersEl.innerHTML = "";
  }
}

// Drag-and-drop onto the stage, plus the toolbar button / hidden file input.
let dragDepth = 0; // dragenter/leave fire per child; count to toggle the hint once
function onStageDragEnter(e) {
  if (!inSession || !fileCaps || modalOpen) return;
  if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
  e.preventDefault();
  dragDepth += 1;
  if (dropHint) dropHint.hidden = false;
}
function onStageDragOver(e) {
  if (!inSession || !fileCaps || modalOpen) return;
  if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
  e.preventDefault(); // required so the "drop" event fires
  e.dataTransfer.dropEffect = "copy";
}
function onStageDragLeave() {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0 && dropHint) dropHint.hidden = true;
}
function onStageDrop(e) {
  dragDepth = 0;
  if (dropHint) dropHint.hidden = true;
  if (!inSession || !fileCaps || modalOpen) return;
  if (!e.dataTransfer) return;
  e.preventDefault();
  const files = Array.from(e.dataTransfer.files || []);
  for (const f of files) enqueueUpload(f);
}
function onSendFileClick() {
  if (fileInput) fileInput.click();
}
function onFileInputChange() {
  const files = Array.from(fileInput.files || []);
  for (const f of files) enqueueUpload(f);
  fileInput.value = ""; // allow re-picking the same file
}

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
  if (statusEl.parentElement !== barLeft) barLeft.appendChild(statusEl); // next to the brand
}
placeStatusInJoin(); // start on the join screen

function showJoinError(msg) {
  const e = $("joinError");
  e.textContent = msg;
  e.hidden = false;
}

// ---------------- PIN gate (host auto-accepts behind a PIN) ----------------
// When the agent auto-accepts but requires a PIN, it answers our join with
// "pin-required" instead of "monitors". We swap the code card for a PIN card; a
// correct PIN yields "monitors" (session starts), a wrong one "pin-bad" (retry).
function showPinGate(len) {
  pinActive = true;
  pinLen = Number.isInteger(len) && len > 0 ? len : 4;
  pinInput.maxLength = pinLen;
  pinInput.placeholder = "·".repeat(pinLen);
  pinInput.value = "";
  pinDesc.textContent = t("connect.pin.desc").replace("{len}", pinLen);
  pinError.hidden = true;
  joinCard.hidden = true;
  pinCard.hidden = false;
  setStatus(t("connect.pin.title"), "warn");
  try {
    pinInput.focus();
  } catch {
    /* ignore */
  }
}

function hidePinGate() {
  pinActive = false;
  pinCard.hidden = true;
  joinCard.hidden = false;
}

function showPinError(msg) {
  pinError.textContent = msg;
  pinError.hidden = false;
}

pinForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const pin = pinInput.value.trim();
  if (pin.length !== pinLen || ![...pin].every((c) => c >= "0" && c <= "9")) {
    showPinError(t("connect.err.pinLen").replace("{len}", pinLen));
    return;
  }
  pinError.hidden = true;
  send({ type: "pin", pin });
  setStatus(t("connect.pin.checking"), "warn");
});

// Session-code alphabet (must match the Worker's CODE_ALPHABET): uppercase letters +
// digits minus the visually ambiguous 0/O/1/I/L. Validated client-side so a malformed
// code shows a clear "invalid code" instead of failing the WS upgrade (a generic close
// the viewer would otherwise mistake for an interruption).
const CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";

$("joinForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const code = $("codeInput").value.trim().toUpperCase();
  if (code.length !== 10) {
    showJoinError(t("connect.err.codeLen"));
    return;
  }
  if (![...code].every((c) => CODE_ALPHABET.includes(c))) {
    showJoinError(t("connect.err.codeInvalid"));
    return;
  }
  $("joinError").hidden = true;
  connect(code);
});

function connect(code) {
  // A fresh, user-initiated join is NOT a reconnection. Clear any in-flight
  // interruption state first, so THIS attempt's own outcome (no-agent / busy) surfaces
  // as a join error instead of being swallowed by the reconnect machinery. Without
  // this, submitting a new or mistyped code while a pre-session interruption is active
  // (the join form stays live until a session is established) would be mis-read as a
  // "connection interrupted" and spin on reconnect until the timeout.
  interruptionActive = false;
  stopReconnecting();
  hidePinGate(); // a fresh user-initiated join always starts on the code card
  if (dialogTimer) {
    clearTimeout(dialogTimer);
    dialogTimer = null;
  }
  if (reaskTimer) {
    clearTimeout(reaskTimer);
    reaskTimer = null;
  }
  if (dismissModal) dismissModal();
  // Tear down any socket from a previous attempt before opening a new one. Leaving it
  // open orphans it on the server: a same-code re-submit would see its own still-live
  // socket and be refused "busy", and its later onclose (intentionalClose was false)
  // would spuriously interrupt or end this fresh attempt.
  if (ws) {
    intentionalClose = true;
    try {
      ws.close();
    } catch {
      /* ignore */
    }
    ws = null;
  }
  // Start this attempt's liveness flags clean, so a leftover agent/media-down from a
  // pre-session interruption can't make the first heartbeat tick re-enter an
  // interruption and swallow this attempt's no-agent/busy error.
  agentDown = false;
  mediaDown = false;
  videoEverPlayed = false;
  currentCode = code;
  // A user-initiated join is a NEW session relationship: mint a fresh resume token. The
  // reconnect path reuses it (via openWs) so a dropped session resumes without a new
  // prompt, but a brand-new join re-earns consent.
  vtok = newVtok();
  live = true;
  setStatus(t("connect.status.connecting"));
  openWs(code);
  startHeartbeat();
}

// (Re)open the viewer WebSocket. Used by connect() and by the reconnection loop.
function openWs(code) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  intentionalClose = false;
  // Carry our resume token so the agent can recognise a reconnect as the same approved
  // viewer. It is reused across reconnects of this session (minted in connect()).
  const q = vtok ? `&vtok=${encodeURIComponent(vtok)}` : "";
  ws = new WebSocket(`${proto}://${location.host}/ws/viewer?code=${code}${q}`);
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
      // The agent's own link to the server dropped; our link is fine.
      agentDown = true;
      if (!inSession) {
        // We never got a session going — we're still waiting to be accepted and the
        // agent already dropped. It isn't responding (it may have hung, or a previous
        // session is stuck). There's nothing established to reconnect to, so DON'T sit
        // on "waiting to be accepted" or pop the reconnect dialog: show a clear error
        // and reset to the join form so the user can retry.
        abortConnect();
        setStatus(t("connect.status.couldNotConnect"), "bad");
        showJoinError(t("connect.err.notResponding"));
        break;
      }
      // An established session blipped: warn + wait for the agent to reconnect (the DO
      // holds the session during its grace).
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
    case "pin-required":
      // The host auto-accepts but gates admission behind a PIN. Ignore for an already
      // live session (control messages relay to all viewers). If the gate is already up
      // (e.g. the agent re-served us after an own-link blip), just re-assert the status —
      // don't re-render, which would wipe any digits already typed.
      if (!inSession) {
        if (pinActive) setStatus(t("connect.pin.title"), "warn");
        else showPinGate(msg.len);
      }
      break;
    case "pin-bad":
      // Wrong PIN, but attempts remain: let the user retry in place.
      if (pinActive) {
        const left = Number.isInteger(msg.left) ? msg.left : 0;
        showPinError(t("connect.pin.bad").replace("{left}", left));
        pinInput.value = "";
        try {
          pinInput.focus();
        } catch {
          /* ignore */
        }
        setStatus(t("connect.pin.title"), "warn");
      }
      break;
    case "denied":
      if (pinActive) {
        // The PIN challenge ended in refusal (too many wrong tries, a lockout, or a
        // timeout). Return to the code form with a clear message; a fresh join restarts
        // the challenge (the host may have changed the PIN).
        setStatus(t("connect.pin.failed"), "bad");
        hidePinGate();
        showJoinError(t("connect.pin.failed"));
        abortConnect();
      } else {
        setStatus(t("connect.status.declined"), "bad");
        endSession(true);
      }
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
    case "caps":
      // The agent advertised its optional capabilities (clipboard / file transfer).
      clipCaps = !!msg.clip;
      fileCaps = !!msg.file;
      updateTransferUI();
      break;
    case "clip":
      // The host clipboard changed; mirror it into ours (best-effort).
      if (typeof msg.text === "string") applyRemoteClipboard(msg.text);
      break;
    case "clip-img-begin":
      onClipImgBegin(msg);
      break;
    case "clip-img-chunk":
      onClipImgChunk(msg);
      break;
    case "clip-img-end":
      onClipImgEnd(msg);
      break;
    case "file-begin":
      onFileBegin(msg);
      break;
    case "file-chunk":
      onFileChunk(msg);
      break;
    case "file-end":
      onFileEnd(msg);
      break;
    case "file-cancel":
      onFileCancel(msg);
      break;
    case "file-ack":
      onFileAck(msg);
      break;
  }
}

function updateTransferUI() {
  const show = !(inSession && fileCaps);
  if (sendFileBtn) sendFileBtn.hidden = show;
  if (fsSendFileBtn) fsSendFileBtn.hidden = show;
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
    // Minimize the receiver's jitter buffer: for remote CONTROL, low latency matters
    // far more than smoothing out jitter, so target ~0 playout delay. jitterBufferTarget
    // is the current API; playoutDelayHint is the older Chromium fallback. Both no-op
    // where unsupported (Firefox), leaving the browser's adaptive default.
    try { e.receiver.jitterBufferTarget = 0; } catch { /* unsupported */ }
    try {
      if ("playoutDelayHint" in e.receiver) e.receiver.playoutDelayHint = 0;
    } catch { /* unsupported */ }
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
    lastVideoAdvance = performance.now(); // fresh stream: reset the stall grace
  };

  pc.onicecandidate = (e) => {
    if (e.candidate) send({ type: "webrtc-ice", candidate: e.candidate });
  };

  // The WebSocket heartbeat is the primary liveness signal for OUR link to the server.
  // But when the AGENT's link drops (half-open), our WS + pong stay healthy, so the
  // heartbeat can't see it — only the video stalls. The DO eventually relays
  // "agent-interrupted", but that can be slow on a half-open agent drop, so we also
  // watch the peer connection: a sustained "failed"/"disconnected" means the media is
  // gone. We treat that as an INTERRUPTION (warn + wait for the agent to re-serve),
  // NOT an immediate disconnect — mediaDown is folded into isDown()/isUp(), and a new
  // offer that reconnects the media clears it. A brief, self-recovering ICE blip
  // (back to "connected" within the grace) is ignored.
  pc.onconnectionstatechange = () => {
    if (!inSession || !pc) return;
    const st = pc.connectionState;
    if (st === "connected") {
      mediaDown = false; // media is flowing again
      lastVideoAdvance = performance.now(); // media path up: grace until frames play
      if (mediaFailTimer) {
        clearTimeout(mediaFailTimer);
        mediaFailTimer = null;
      }
    } else if (st === "failed" || st === "disconnected") {
      if (!mediaFailTimer && !mediaDown) {
        mediaFailTimer = setTimeout(() => {
          mediaFailTimer = null;
          // Still down after the grace: mark the media down so the heartbeat tick
          // treats it as an interruption (onInterruption -> warn + wait for re-serve).
          if (
            inSession && pc &&
            (pc.connectionState === "failed" || pc.connectionState === "disconnected")
          ) {
            mediaDown = true;
          }
        }, WARN_DELAY_MS);
      }
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

// The video stopped advancing while we're streaming: the agent's frames stopped
// arriving (an agent-side drop), even though our own WS + pong can still be healthy
// (the DO is slow to notice a half-open agent). This is the fast signal for an
// agent-side drop — far quicker than waiting for WebRTC ICE to reach "failed".
function videoStalled() {
  // Only trust the frame signal while the page is visible: requestVideoFrameCallback
  // is driven by the compositor, which PAUSES for a hidden tab even though the WS/pong
  // stay alive — so a backgrounded tab must NOT read a frozen rVFC as a real stall.
  return (
    inSession && videoEverPlayed && screens.size > 0 && !document.hidden &&
    performance.now() - lastVideoAdvance > STALE_MS
  );
}

// Returning to the foreground: rVFC was paused while hidden, so lastVideoAdvance is
// stale. Refresh the grace so the first tick back doesn't flash a false stall (rVFC
// resumes within a frame). The pong path keeps working while hidden, so leave it.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) lastVideoAdvance = performance.now();
});
// Our link to the server is unusable right now (closed, or no pong in time, or the
// agent's side dropped, or the video stalled). A CONNECTING socket is neither down nor
// up yet — we hold.
function isDown() {
  if (agentDown) return true;
  if (mediaDown) return true; // the WebRTC media failed (agent likely gone)
  if (videoStalled()) return true; // frames stopped arriving (agent-side drop)
  if (!ws) return true;
  if (ws.readyState === 0) return false; // opening: onclose/onerror catches failures
  if (ws.readyState !== 1) return true; // closing / closed
  return performance.now() - lastPong > INTERRUPT_MS;
}
// Our link is confirmed healthy again (used to detect recovery): WS open with a recent
// pong, the agent up, and — once lost — the video flowing again.
function isUp() {
  if (agentDown || mediaDown) return false;
  if (videoStalled()) return false;
  return !!ws && ws.readyState === 1 && performance.now() - lastPong <= INTERRUPT_MS;
}

// Is our OWN socket+link to the server healthy (independent of the agent/media)?
// Drives whether reconnection should reopen the WS or just wait for the agent.
function ourLinkHealthy() {
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
    lastVideoAdvance = now; // video "stalled" only because JS wasn't running
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
  mediaDown = false;
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
    // link has recovered and re-interrupt the session we just got back.
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
  // Reopen OUR WS only when our own link to the server is down. If the agent dropped
  // or only the media failed (our WS is fine), just wait for the agent to re-serve;
  // reopening a healthy socket would hit the DO's "busy" check.
  if (!ourLinkHealthy()) {
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
  hidePinGate(); // a terminal join failure (no-agent/busy/agent gone) returns to the code card
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
  clipLast = null; // fresh session: no clipboard reconciled yet (caps arrives right after)
  clipLastImg = null;
  clipImgRx = null;
  fileCaps = false; // re-advertised by the agent's "caps" message right after "monitors"
  cancelAllTransfers();
  updateTransferUI();

  buildScreens();

  // Default view: grid when there is more than one monitor, else single.
  viewMode = monitors.length > 1 ? "grid" : "single";
  selectedIndex = monitors.length ? monitors[0].i : 0;

  hidePinGate(); // correct PIN (or no PIN): leave the gate and show the stage
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

  // The new session's <video>s haven't played yet; the stall check stays off until the
  // first real frame (videoEverPlayed) so slow WebRTC setup can't look like a stall.
  videoEverPlayed = false;
  lastVideoAdvance = performance.now();

  sendWant();
}

function endSession(keepStatus) {
  inSession = false;
  live = false;
  interruptionActive = false;
  agentDown = false;
  mediaDown = false;
  videoEverPlayed = false;
  clipCaps = false; // the agent is gone; stop sampling the clipboard until a new session
  clipLast = null;
  clipLastImg = null;
  clipImgRx = null;
  fileCaps = false;
  cancelAllTransfers();
  updateTransferUI();
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
  hidePinGate(); // never leave the PIN card over the code form
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
  // Terminal JOIN failures the DO signals by close code: no-agent (4404) / busy (4409).
  // The DO sends an {error} message then closes, but if the close races the message the
  // message can be dropped — so handle the code here too, otherwise a bad/unknown code
  // would fall through to onInterruption() and wrongly read as "connection interrupted".
  // Only on the INITIAL join, though: a RECONNECTING viewer can legitimately get 4409
  // ("busy") from its own not-yet-reaped socket, and must keep retrying, not give up.
  if (ev && (ev.code === 4404 || ev.code === 4409) && !interruptionActive) {
    abortConnect();
    setStatus(t("connect.status.couldNotConnect"), "bad");
    showJoinError(
      ev.code === 4409 ? t("connect.err.busy") : t("connect.err.noAgent"),
    );
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
    // Liveness: requestVideoFrameCallback fires once per RENDERED frame and stops when
    // frames stop arriving (an agent-side drop). timeupdate is NOT usable here — a
    // MediaStream <video>'s currentTime keeps advancing on the element's own clock even
    // when no frames come in. Browsers without rVFC (Firefox) fall back to the slower
    // WebRTC-"failed" (mediaDown) signal; videoEverPlayed simply stays false there.
    trackVideoFrames(el);
    const handlers = attachInput(el, m);
    screensEl.appendChild(el);
    screens.set(m.i, { el, handlers, mon: m });
  }
}

function trackVideoFrames(el) {
  if (typeof el.requestVideoFrameCallback !== "function") return;
  const cb = () => {
    lastVideoAdvance = performance.now();
    videoEverPlayed = true;
    if (el.isConnected) {
      try {
        el._rvfc = el.requestVideoFrameCallback(cb);
      } catch {
        /* ignore */
      }
    }
  };
  try {
    el._rvfc = el.requestVideoFrameCallback(cb);
  } catch {
    /* ignore */
  }
}

function clearScreens() {
  for (const [, s] of screens) {
    const h = s.handlers;
    s.el.removeEventListener("mousemove", h.onMove);
    s.el.removeEventListener("mousedown", h.onDown);
    s.el.removeEventListener("wheel", h.onWheel);
    s.el.removeEventListener("contextmenu", h.onCtx);
    // Cancel the pending rVFC so the orphaned (detached, stream-less) <video> — which
    // would never fire its callback again — doesn't keep its closure (and itself) alive.
    if (s.el._rvfc != null && typeof s.el.cancelVideoFrameCallback === "function") {
      try {
        s.el.cancelVideoFrameCallback(s.el._rvfc);
      } catch {
        /* ignore */
      }
    }
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
    barActions.insertBefore(viewControls, kbSwitch);
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
    // The Input switch (kbToggle) gates ALL input — mouse and keyboard. When off, this
    // is a view-only session: don't forward movement and show a plain cursor.
    if (!kbToggle.checked) {
      el.style.cursor = "default";
      return;
    }
    const p = mediaXY(el, mon, e);
    // Over the agent's own window the agent ignores clicks, so hint with a
    // "not-allowed" cursor (updated every move; "" reverts to the CSS arrow cursor).
    el.style.cursor = inBlockRect(mon.i, p.x, p.y) ? "not-allowed" : "";
    const now = performance.now();
    if (now - lastMove < 15) return; // ~66 moves/sec max (snappier remote cursor)
    lastMove = now;
    send({ t: "mm", m: mon.i, x: p.x, y: p.y });
  };
  const onDown = (e) => {
    e.preventDefault();
    if (!kbToggle.checked) return; // input off: view-only
    el.focus();
    activeDown = { el, mon };
    const p = mediaXY(el, mon, e);
    send({ t: "md", m: mon.i, x: p.x, y: p.y, b: e.button });
  };
  const onWheel = (e) => {
    e.preventDefault();
    if (!kbToggle.checked) return; // input off: view-only
    const p = mediaXY(el, mon, e);
    // Normalize the delta to a pixel-ish unit regardless of deltaMode (0=pixel,
    // 1=line, 2=page) so the agent applies a consistent amount across browsers and
    // devices; the agent sums these deltas into whole scroll steps (not one click
    // per event), which is what keeps scrolling from being hypersensitive.
    const unit = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 800 : 1;
    send({ t: "scroll", m: mon.i, x: p.x, y: p.y, dx: e.deltaX * unit, dy: e.deltaY * unit });
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
  // Clipboard up-sync triggers: focus/visibility (silent once permission is granted) and
  // pointerdown (a user gesture that lets Chrome show the one-time clipboard-read prompt).
  window.addEventListener("focus", onClipFocus);
  document.addEventListener("visibilitychange", onClipVisible);
  window.addEventListener("pointerdown", onClipPointerDown, true);
  // File drag-and-drop onto the screen stage.
  if (stageEl) {
    stageEl.addEventListener("dragenter", onStageDragEnter);
    stageEl.addEventListener("dragover", onStageDragOver);
    stageEl.addEventListener("dragleave", onStageDragLeave);
    stageEl.addEventListener("drop", onStageDrop);
  }
}

function unbindWindowListeners() {
  if (!windowBound) return;
  windowBound = false;
  window.removeEventListener("mouseup", onWindowMouseUp);
  window.removeEventListener("keydown", onKeyDown, true);
  window.removeEventListener("keyup", onKeyUp, true);
  window.removeEventListener("focus", onClipFocus);
  document.removeEventListener("visibilitychange", onClipVisible);
  window.removeEventListener("pointerdown", onClipPointerDown, true);
  if (stageEl) {
    stageEl.removeEventListener("dragenter", onStageDragEnter);
    stageEl.removeEventListener("dragover", onStageDragOver);
    stageEl.removeEventListener("dragleave", onStageDragLeave);
    stageEl.removeEventListener("drop", onStageDrop);
  }
  dragDepth = 0;
  if (dropHint) dropHint.hidden = true;
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

if (sendFileBtn) sendFileBtn.addEventListener("click", onSendFileClick);
if (fsSendFileBtn) fsSendFileBtn.addEventListener("click", onSendFileClick);
if (fileInput) fileInput.addEventListener("change", onFileInputChange);

// Global drag guard (always on): stop the browser from navigating away — which would
// destroy a live session — if a file is dropped anywhere outside the stage drop zone
// (e.g. on the top bar). The stage's own drop handler still performs the upload; drops
// elsewhere are simply swallowed. preventDefault at window level still cancels the
// default open/navigate even though the stage handler runs first on an in-stage drop.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

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
