/**
 * Local WhatsApp bridge — sends PDF invoices via your logged-in WhatsApp Web session.
 * Run once, scan QR code, then bills can be sent automatically from the billing app.
 */
const express = require("express");
const path = require("path");
const fs = require("fs");

/** Lazy-load heavy deps so /health responds within seconds on Railway. */
let Client = null;
let LocalAuth = null;
let MessageMedia = null;
let QRCode = null;
let wwebjsLoaded = false;

function ensureWwebjs() {
  if (wwebjsLoaded) return;
  ({ Client, LocalAuth, MessageMedia } = require("whatsapp-web.js"));
  QRCode = require("qrcode");
  wwebjsLoaded = true;
  console.log("[WhatsApp] Libraries loaded.");
}

process.on("uncaughtException", (err) => {
  console.error("[WhatsApp] Uncaught exception:", err?.message || err);
  state.phase = "error";
  state.lastError = `Scanner crashed: ${err?.message || err}. Restarting…`;
  initInProgress = false;
  setTimeout(() => {
    forceFreshQrLink("uncaught").catch((e) => console.error("[WhatsApp] Recovery failed:", e.message));
  }, 2000);
});

process.on("unhandledRejection", (reason) => {
  console.error("[WhatsApp] Unhandled rejection:", reason);
});

const PORT = Number(process.env.WHATSAPP_BRIDGE_PORT || 3001);
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, "..", "data");
const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || path.join(DATA_DIR, "whatsapp-auth");
const CACHE_DIR = process.env.WHATSAPP_CACHE_DIR || path.join(DATA_DIR, "whatsapp-cache");
const LEGACY_AUTH_DIR = path.join(__dirname, ".wwebjs_auth");
const IS_HOSTED = Boolean(
  process.env.RAILWAY_ENVIRONMENT || process.env.PUPPETEER_EXECUTABLE_PATH
);

/**
 * Pin a known WhatsApp Web HTML (kept under CACHE_DIR). The `ready` event
 * often never fires on multi-device WA Web — we treat CONNECTED state as ready.
 */
const WA_WEB_VERSION =
  process.env.WHATSAPP_WEB_VERSION || "2.3000.1043441279-alpha";
const BUNDLED_CACHE_DIR = path.join(__dirname, "wa-cache");

const AUTH_READY_TIMEOUT_MS = Number(
  process.env.WHATSAPP_AUTH_TIMEOUT_MS || (IS_HOSTED ? 420000 : 180000)
);
const RESTORE_QR_GRACE_MS = Number(process.env.WHATSAPP_RESTORE_GRACE_MS || (IS_HOSTED ? 0 : 60000));
const RESTORE_FAIL_MS = Number(process.env.WHATSAPP_RESTORE_FAIL_MS || (IS_HOSTED ? 12000 : 150000));
const QR_STARTUP_TIMEOUT_MS = Number(process.env.WHATSAPP_QR_TIMEOUT_MS || (IS_HOSTED ? 45000 : 120000));
const INIT_TIMEOUT_MS = Number(process.env.WHATSAPP_INIT_TIMEOUT_MS || (IS_HOSTED ? 90000 : 90000));
const CLIENT_ID = process.env.WHATSAPP_CLIENT_ID || "rinse-rise";

const state = {
  ready: false,
  qr: null,
  lastError: null,
  phase: "booting",
  loadingPercent: 0,
  authenticatingSince: null,
  waState: null,
  sessionLinked: false,
  qrGeneration: 0,
};

let authTimer = null;
let connectPollTimer = null;
let restoreWatchdogTimer = null;
let qrStartupTimer = null;
let qrDuringRestoreCount = 0;
let client = null;
let recovering = false;
let sendInProgress = false;
let sendInProgressSince = 0;
const SEND_LOCK_MAX_MS = Number(process.env.WHATSAPP_SEND_LOCK_MS || 60000);
const SEND_OPERATION_TIMEOUT_MS = Number(process.env.WHATSAPP_SEND_TIMEOUT_MS || (IS_HOSTED ? 90000 : 60000));
const GET_NUMBER_ID_TIMEOUT_MS = Number(process.env.WHATSAPP_NUMBER_LOOKUP_MS || 5000);

const LOCK_FILE = path.join(AUTH_DIR, ".bridge.lock");
const SESSION_LINKED_FILE = path.join(AUTH_DIR, ".session-linked");
let lastReconnectAt = 0;
let bridgeStartedAt = Date.now();
let initInProgress = false;
let freshQrInFlight = false;

function ensureAuthDirs() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  fs.mkdirSync(CACHE_DIR, { recursive: true });
}

/** Ship a pinned WA Web HTML in the repo so hosted deploys never fetch a broken remote build. */
function ensureWaWebCache() {
  ensureAuthDirs();
  const target = path.join(CACHE_DIR, `${WA_WEB_VERSION}.html`);
  const bundled = path.join(BUNDLED_CACHE_DIR, `${WA_WEB_VERSION}.html`);
  if (!fs.existsSync(bundled)) {
    console.warn(`[WhatsApp] Bundled WA Web HTML missing: ${bundled}`);
    return;
  }
  const needsCopy =
    !fs.existsSync(target) || fs.statSync(bundled).size !== fs.statSync(target).size;
  if (needsCopy) {
    fs.copyFileSync(bundled, target);
    console.log(`[WhatsApp] Seeded WA Web cache: ${WA_WEB_VERSION}`);
  }
}

function clearQrStartupWatchdog() {
  if (qrStartupTimer) {
    clearTimeout(qrStartupTimer);
    qrStartupTimer = null;
  }
}

function scheduleQrStartupWatchdog() {
  clearQrStartupWatchdog();
  qrStartupTimer = setTimeout(() => {
    if (state.ready || state.qr) return;
    console.warn("[WhatsApp] No QR generated yet — clearing session and retrying…");
    void forceFreshQrLink("qr-startup-timeout");
  }, QR_STARTUP_TIMEOUT_MS);
}

function clearRestoreWatchdog() {
  if (restoreWatchdogTimer) {
    clearTimeout(restoreWatchdogTimer);
    restoreWatchdogTimer = null;
  }
}

function scheduleRestoreWatchdog() {
  clearRestoreWatchdog();
  if (!hasSessionLinked()) return;
  restoreWatchdogTimer = setTimeout(() => {
    if (state.ready) return;
    console.warn("[WhatsApp] Saved session did not restore in time — clearing for fresh QR…");
    void forceFreshQrLink("restore-timeout");
  }, RESTORE_FAIL_MS);
}

async function forceFreshQrLink(reason) {
  if (freshQrInFlight) return;
  freshQrInFlight = true;
  if (IS_HOSTED && reason === "qr-startup-timeout") {
    process.env.WHATSAPP_REMOTE_CACHE = "1";
  }
  clearRestoreWatchdog();
  clearQrStartupWatchdog();
  clearSessionLinked();
  state.ready = false;
  state.qr = null;
  state.phase = "starting";
  state.lastError = "Generating QR code — keep this window open…";
  qrDuringRestoreCount = 0;
  console.warn("[WhatsApp] Forcing fresh QR link:", reason);
  try {
    await destroyClient();
    await initializeClient({ fresh: true });
  } catch (err) {
    state.phase = "error";
    state.lastError = err.message || "Could not restart WhatsApp scanner.";
    console.error("[WhatsApp] Fresh QR restart failed:", err.message);
  } finally {
    freshQrInFlight = false;
  }
}

/** Move session from old whatsapp-bridge/.wwebjs_auth to data/whatsapp-auth once. */
function migrateLegacyAuthDir() {
  if (!LEGACY_AUTH_DIR || path.resolve(LEGACY_AUTH_DIR) === path.resolve(AUTH_DIR)) return;
  try {
    const legacySession = path.join(LEGACY_AUTH_DIR, "session-rinse-rise");
    const newSession = path.join(AUTH_DIR, "session-rinse-rise");
    if (fs.existsSync(legacySession) && !fs.existsSync(newSession)) {
      console.log("[WhatsApp] Migrating saved session to persistent data folder…");
      fs.cpSync(LEGACY_AUTH_DIR, AUTH_DIR, { recursive: true });
    } else if (fs.existsSync(path.join(LEGACY_AUTH_DIR, ".session-linked")) && !hasSessionLinked()) {
      fs.copyFileSync(
        path.join(LEGACY_AUTH_DIR, ".session-linked"),
        path.join(AUTH_DIR, ".session-linked")
      );
    }
  } catch (err) {
    console.warn("[WhatsApp] Legacy auth migration:", err.message);
  }
}

function markSessionLinked() {
  try {
    ensureAuthDirs();
    fs.writeFileSync(SESSION_LINKED_FILE, new Date().toISOString());
    state.sessionLinked = true;
  } catch {
    /* ignore */
  }
}

function clearSessionLinked() {
  try {
    if (fs.existsSync(SESSION_LINKED_FILE)) fs.unlinkSync(SESSION_LINKED_FILE);
  } catch {
    /* ignore */
  }
  state.sessionLinked = false;
}

function hasSessionLinked() {
  return fs.existsSync(SESSION_LINKED_FILE);
}

function processAlive(pid) {
  if (!pid || Number.isNaN(pid)) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function acquireSingleInstanceLock() {
  ensureAuthDirs();
  if (fs.existsSync(LOCK_FILE)) {
    const existing = parseInt(String(fs.readFileSync(LOCK_FILE, "utf8")).trim(), 10);
    if (processAlive(existing) && existing !== process.pid) {
      console.error(`[WhatsApp] Bridge already running (pid ${existing}). Exiting duplicate.`);
      process.exit(0);
    }
    fs.unlinkSync(LOCK_FILE);
  }
  fs.writeFileSync(LOCK_FILE, String(process.pid));
}

function releaseSingleInstanceLock() {
  try {
    if (fs.existsSync(LOCK_FILE)) {
      const existing = parseInt(String(fs.readFileSync(LOCK_FILE, "utf8")).trim(), 10);
      if (existing === process.pid) fs.unlinkSync(LOCK_FILE);
    }
  } catch {
    /* ignore */
  }
}

/** Hosted: system Chromium (fastest on Docker). Local Windows: system Chrome/Edge. */
function resolveChromePath() {
  if (IS_HOSTED) {
    const candidates = [
      process.env.PUPPETEER_EXECUTABLE_PATH,
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
      "/usr/bin/google-chrome-stable",
    ].filter(Boolean);
    for (const candidate of candidates) {
      try {
        if (fs.existsSync(candidate)) return candidate;
      } catch {
        /* ignore */
      }
    }
    try {
      const puppeteer = require("puppeteer");
      const bundled = puppeteer.executablePath();
      if (bundled && fs.existsSync(bundled)) return bundled;
    } catch (err) {
      console.warn("[WhatsApp] Puppeteer bundled Chrome lookup:", err.message);
    }
    return "";
  }

  if (process.env.PUPPETEER_EXECUTABLE_PATH && fs.existsSync(process.env.PUPPETEER_EXECUTABLE_PATH)) {
    return process.env.PUPPETEER_EXECUTABLE_PATH;
  }
  const candidates = [
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "Google", "Chrome", "Application", "chrome.exe"),
    process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, "Google", "Chrome", "Application", "chrome.exe"),
    process.env["PROGRAMFILES(X86)"] && path.join(process.env["PROGRAMFILES(X86)"], "Google", "Chrome", "Application", "chrome.exe"),
    process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, "Microsoft", "Edge", "Application", "msedge.exe"),
    process.env["PROGRAMFILES(X86)"] && path.join(process.env["PROGRAMFILES(X86)"], "Microsoft", "Edge", "Application", "msedge.exe"),
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {
      /* ignore */
    }
  }
  return "";
}

let chromePathCache = null;
function getChromePath() {
  if (chromePathCache === null) {
    chromePathCache = resolveChromePath();
  }
  return chromePathCache;
}

function resetChromePathCache() {
  chromePathCache = null;
}

const app = express();
app.use(express.json({ limit: "2mb" }));

function clearConnectPoll() {
  if (connectPollTimer) {
    clearInterval(connectPollTimer);
    connectPollTimer = null;
  }
}

function markReady(source) {
  clearTimeout(authTimer);
  clearConnectPoll();
  clearRestoreWatchdog();
  clearQrStartupWatchdog();
  state.qr = null;
  state.qrGeneration = 0;
  qrDuringRestoreCount = 0;
  state.lastError = null;
  state.loadingPercent = 100;
  state.phase = "connecting";
  state.authenticatingSince = null;
  markSessionLinked();
  console.log(`[WhatsApp] CONNECTED (${source}) — waiting for chat store & comms…`);

  waitForFullyReady(90000)
    .then((ok) => {
      if (ok) {
        state.phase = "ready";
        state.ready = true;
        state.lastError = null;
        console.log(`[WhatsApp] Connected and ready (${source}).`);
        return;
      }
      state.phase = "loading";
      state.ready = false;
      state.lastError =
        "WhatsApp linked — still finishing setup. Wait about 1 minute, then try sending again.";
      startStoreReadyPoll();
    })
    .catch((err) => {
      console.warn("[WhatsApp] Store wait failed:", err.message);
      state.phase = "loading";
      state.ready = false;
      state.lastError = "WhatsApp linked — still starting up. Wait a minute and try again.";
      startStoreReadyPoll();
    });
}

function startConnectedPoll(waClient) {
  clearConnectPoll();
  let connectedTicks = 0;
  connectPollTimer = setInterval(async () => {
    if (state.ready || !waClient) {
      clearConnectPoll();
      return;
    }
    try {
      const waState = await waClient.getState();
      state.waState = waState || null;
      if (waState === "CONNECTED") {
        connectedTicks += 1;
        // Require CONNECTED twice in a row (~4s) — ready event often never fires
        // on newer WA Web builds, but state still settles to CONNECTED.
        if (connectedTicks >= 2) {
          markReady("state-poll");
        }
      } else {
        connectedTicks = 0;
      }
    } catch (err) {
      console.warn("[WhatsApp] State poll:", err.message);
    }
  }, 2000);
}

function scheduleAuthTimeout() {
  clearTimeout(authTimer);
  authTimer = setTimeout(async () => {
    if (state.ready) return;

    // Last chance: if WhatsApp already says CONNECTED, force ready.
    try {
      if (client) {
        const waState = await client.getState();
        state.waState = waState || null;
        if (waState === "CONNECTED") {
          markReady("auth-timeout-connected");
          return;
        }
      }
    } catch (err) {
      console.warn("[WhatsApp] Auth timeout getState:", err.message);
    }

    state.phase = "error";
    state.lastError =
      "Phone linked but WhatsApp Web did not finish loading. Click Reset & Scan Again, wait for a fresh QR, scan quickly, then keep this window open for up to 3 minutes.";
    console.error("[WhatsApp] Ready timed out after authentication.");
    clearConnectPoll();
  }, AUTH_READY_TIMEOUT_MS);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function clearSendLockIfStale() {
  if (!sendInProgress) return;
  if (Date.now() - sendInProgressSince > SEND_LOCK_MAX_MS) {
    console.warn("[WhatsApp] Cleared stale send lock after timeout.");
    sendInProgress = false;
    sendInProgressSince = 0;
  }
}

function acquireSendLock() {
  clearSendLockIfStale();
  if (sendInProgress) return false;
  sendInProgress = true;
  sendInProgressSince = Date.now();
  return true;
}

function releaseSendLock() {
  sendInProgress = false;
  sendInProgressSince = 0;
}

function sendBusyResponse(res) {
  clearSendLockIfStale();
  if (!sendInProgress) {
    return res.status(503).json({
      error: "WhatsApp send slot was busy but is free now — please try again.",
      retryAfterSec: 1,
    });
  }
  const busyForSec = sendInProgressSince
    ? Math.max(1, Math.ceil((Date.now() - sendInProgressSince) / 1000))
    : 0;
  return res.status(429).json({
    error:
      busyForSec >= 8
        ? "WhatsApp is still sending the previous message — wait a few seconds and try again."
        : "Another WhatsApp send is in progress. Please wait a moment.",
    retryAfterSec: Math.min(8, Math.max(2, 6 - busyForSec)),
    busyForSec,
  });
}

async function withSendTimeout(promise, label = "send") {
  let timer = null;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          reject(new Error(`WhatsApp ${label} timed out after ${Math.round(SEND_OPERATION_TIMEOUT_MS / 1000)}s. Try again.`));
        }, SEND_OPERATION_TIMEOUT_MS);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function isCommsError(err) {
  const msg = String(err?.message || err).toLowerCase();
  return (
    msg.includes("startcomms") ||
    msg.includes("sendiq") ||
    msg.includes("[comms]") ||
    msg.includes("not ready")
  );
}

function isLidError(err) {
  const msg = String(err?.message || err).toLowerCase();
  return msg.includes("lid is missing") || msg.includes("no lid for user");
}

function isContactGetterError(err) {
  const msg = String(err?.message || err).toLowerCase();
  return (
    msg.includes("data passed to getter") ||
    msg.includes("memoize") ||
    msg.includes("getcontact") ||
    (msg.includes("evaluation failed") && msg.includes("undefined"))
  );
}

function isSessionError(err) {
  const msg = String(err?.message || err).toLowerCase();
  return (
    isCommsError(err) ||
    isStoreError(err) ||
    msg.includes("detached frame") ||
    msg.includes("target closed") ||
    msg.includes("session closed") ||
    msg.includes("protocol error") ||
    msg.includes("execution context was destroyed") ||
    msg.includes("page has been closed") ||
    msg.includes("browser has disconnected")
  );
}

function isStoreError(err) {
  const msg = String(err?.message || err).toLowerCase();
  return (
    err?.code === "STORE_NOT_READY" ||
    msg.includes("getchat") ||
    msg.includes("cannot read properties of undefined") ||
    msg.includes("chat store") ||
    msg.includes("still loading")
  );
}

async function isWhatsAppStoreReady() {
  if (!client?.pupPage) return false;
  try {
    return await client.pupPage.evaluate(() => {
      try {
        const collections = window.require?.("WAWebCollections");
        const chat = collections?.Chat;
        return Boolean(chat && typeof chat.get === "function");
      } catch {
        return false;
      }
    });
  } catch {
    return false;
  }
}

async function waitForStoreReady(timeoutMs = 60000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await isWhatsAppStoreReady()) return true;
    await sleep(1000);
  }
  return false;
}

/** sendIq fails with startComms if the socket layer is not up yet. */
async function isCommsReady() {
  if (!client?.pupPage) return false;
  try {
    return await client.pupPage.evaluate(() => {
      try {
        const Conn = window.require?.("WAWebConnModel")?.Conn;
        if (Conn?.connected || Conn?.wid) return true;
        const me = window.require?.("WAWebUserPrefsMeUser")?.getMaybeMePnUser?.();
        if (me) return true;
        const stream = window.require?.("WAWebStreamModel")?.Stream;
        if (stream?.mode === "MAIN" || stream?.uiActive) return true;
        return false;
      } catch {
        return false;
      }
    });
  } catch {
    return false;
  }
}

async function waitForCommsReady(timeoutMs = 45000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await isCommsReady()) return true;
    await sleep(1500);
  }
  return false;
}

async function waitForFullyReady(timeoutMs = 90000) {
  const storeOk = await waitForStoreReady(Math.min(timeoutMs, 60000));
  if (!storeOk) return false;
  const commsOk = await waitForCommsReady(Math.min(timeoutMs, 45000));
  if (!commsOk) {
    // Hosted Chrome often needs a short grace period after the chat store loads.
    await sleep(8000);
  } else {
    await sleep(2500);
  }
  return (await isWhatsAppStoreReady()) && (await isCommsReady());
}

function startStoreReadyPoll() {
  if (startStoreReadyPoll._timer) return;
  startStoreReadyPoll._timer = setInterval(async () => {
    if (state.ready || !client) {
      clearInterval(startStoreReadyPoll._timer);
      startStoreReadyPoll._timer = null;
      return;
    }
    try {
      if (await isWhatsAppStoreReady() && (await isCommsReady())) {
        state.phase = "ready";
        state.ready = true;
        state.lastError = null;
        console.log("[WhatsApp] Chat store & comms ready.");
        clearInterval(startStoreReadyPoll._timer);
        startStoreReadyPoll._timer = null;
      }
    } catch (err) {
      console.warn("[WhatsApp] Store poll:", err.message);
    }
  }, 2000);
}

function sessionDirPath() {
  return path.join(AUTH_DIR, `session-${CLIENT_ID}`);
}

function validateHostedChrome() {
  if (!IS_HOSTED) return true;
  const chromePath = getChromePath();
  if (chromePath && fs.existsSync(chromePath)) {
    console.log(`[WhatsApp] Using Chrome: ${chromePath}`);
    return true;
  }
  state.phase = "error";
  state.lastError =
    "WhatsApp scanner could not find Chrome on the server. Redeploy the billing app and try again.";
  console.error("[WhatsApp] Chrome binary missing on hosted server.");
  return false;
}

function createClient() {
  ensureWwebjs();
  const useRemoteCache = Boolean(IS_HOSTED && process.env.WHATSAPP_REMOTE_CACHE !== "0");
  const puppeteerConfig = {
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--no-first-run",
      "--mute-audio",
      "--disable-extensions",
      "--disable-background-networking",
      "--disable-blink-features=AutomationControlled",
      "--disable-features=IsolateOrigins,site-per-process,TranslateUI",
      "--disable-ipc-flooding-protection",
      "--disable-renderer-backgrounding",
      "--disable-background-timer-throttling",
      "--disable-software-rasterizer",
      "--window-size=1280,720",
    ],
  };
  const chromePath = getChromePath();
  if (chromePath) {
    puppeteerConfig.executablePath = chromePath;
  }

  const clientOptions = {
    authStrategy: new LocalAuth({ dataPath: AUTH_DIR, clientId: CLIENT_ID }),
    puppeteer: puppeteerConfig,
    takeoverOnConflict: false,
    takeoverTimeoutMs: 0,
  };

  if (useRemoteCache) {
    clientOptions.webVersionCache = {
      type: "remote",
      remotePath:
        "https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/{version}.html",
    };
    console.log("[WhatsApp] Using remote WA Web cache fallback.");
  } else {
    clientOptions.webVersion = WA_WEB_VERSION;
    clientOptions.webVersionCache = {
      type: "local",
      path: CACHE_DIR,
    };
  }

  return new Client(clientOptions);
}

function bindClientEvents(waClient) {
  waClient.on("qr", async (qr) => {
    state.ready = false;
    state.authenticatingSince = null;
    clearTimeout(authTimer);
    clearRestoreWatchdog();

    const linked = hasSessionLinked();
    if (linked && !IS_HOSTED && RESTORE_QR_GRACE_MS > 0) {
      const graceElapsed = Date.now() - bridgeStartedAt >= RESTORE_QR_GRACE_MS;
      if (!graceElapsed) {
        qrDuringRestoreCount += 1;
        if (qrDuringRestoreCount === 1) {
          state.phase = "restoring";
          state.qr = null;
          state.lastError = "Restoring saved WhatsApp session…";
          console.log("[WhatsApp] QR during restore grace — waiting for saved session…");
          startConnectedPoll(waClient);
          return;
        }
      }
    }
    if (linked) clearSessionLinked();

    state.phase = "qr";
    state.loadingPercent = 0;
    state.lastError = null;
    try {
      clearQrStartupWatchdog();
      state.qrGeneration += 1;
      state.qr = await QRCode.toDataURL(qr, { margin: 1, width: 280, errorCorrectionLevel: "M" });
      console.log(`[WhatsApp] QR ready (#${state.qrGeneration}) — scan in billing app.`);
    } catch (err) {
      state.lastError = "Could not render QR code.";
      console.error("[WhatsApp] QR render failed:", err.message);
    }
  });

  waClient.on("loading_screen", (percent, message) => {
    state.loadingPercent = Number(percent) || 0;
    if (state.phase === "qr" && state.qr && !state.ready) {
      if (message) console.log(`[WhatsApp] Loading ${percent}% (QR still visible) — ${message}`);
      return;
    }
    state.phase = "loading";
    state.qr = null;
    if (message) console.log(`[WhatsApp] Loading ${percent}% — ${message}`);
    if (state.loadingPercent >= 90) {
      startConnectedPoll(waClient);
    }
  });

  waClient.on("authenticated", () => {
    state.phase = "authenticating";
    state.qr = null;
    state.lastError = null;
    state.authenticatingSince = Date.now();
    state.loadingPercent = Math.max(state.loadingPercent, 95);
    markSessionLinked();
    console.log("[WhatsApp] Authenticated — waiting for CONNECTED state…");
    scheduleAuthTimeout();
    startConnectedPoll(waClient);
  });

  waClient.on("change_state", (waState) => {
    state.waState = waState;
    console.log("[WhatsApp] State:", waState);
    if (waState === "CONNECTED") {
      markReady("change_state");
    }
  });

  waClient.on("ready", () => {
    markReady("ready-event");
  });

  waClient.on("auth_failure", (msg) => {
    clearTimeout(authTimer);
    state.ready = false;
    state.phase = "error";
    state.lastError = `Authentication failed: ${msg}. Click Reset Connection and scan again.`;
    console.error("[WhatsApp] Auth failure:", msg);
  });

  waClient.on("disconnected", (reason) => {
    clearTimeout(authTimer);
    state.ready = false;
    const reasonText = String(reason || "unknown");
    console.warn("[WhatsApp] Disconnected:", reasonText);

    if (reasonText === "LOGOUT" || reasonText === "UNPAIRED") {
      clearSessionLinked();
      state.phase = "error";
      state.lastError = "Logged out from phone. Click Reset Connection and scan QR again.";
      return;
    }

    state.phase = "disconnected";
    state.lastError = `Reconnecting (${reasonText})…`;
    scheduleReconnect(15000);
  });

  waClient.on("error", (err) => {
    console.error("[WhatsApp] Client error:", err?.message || err);
    if (!state.ready) return;
    if (isSessionError(err)) {
      state.ready = false;
      state.phase = "disconnected";
      state.lastError = "WhatsApp reconnecting…";
      scheduleReconnect(20000);
    }
  });
}

function scheduleReconnect(delayMs) {
  const now = Date.now();
  if (now - lastReconnectAt < 20000) {
    delayMs = Math.max(delayMs, 20000);
  }
  lastReconnectAt = now;
  clearTimeout(scheduleReconnect._timer);
  scheduleReconnect._timer = setTimeout(() => {
    softRecoverClient("disconnect").catch((err) => {
      state.phase = "error";
      state.lastError = err.message || "Could not reconnect WhatsApp.";
      console.error("[WhatsApp] Reconnect failed:", err.message);
    });
  }, delayMs);
}

async function destroyClient() {
  clearTimeout(authTimer);
  clearConnectPoll();
  clearRestoreWatchdog();
  clearQrStartupWatchdog();
  if (!client) return;
  try {
    await client.destroy();
  } catch (err) {
    console.warn("[WhatsApp] Destroy:", err.message);
  }
  client = null;
}

function wipeAuthDir() {
  fs.rmSync(AUTH_DIR, { recursive: true, force: true });
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  clearSessionLinked();
}

async function initializeClient({ fresh = false, _retried = false } = {}) {
  if (initInProgress) return;
  initInProgress = true;
  bridgeStartedAt = Date.now();
  qrDuringRestoreCount = 0;
  state.phase = "starting";
  state.lastError = "Loading WhatsApp libraries…";
  // Yield so /health and /status respond before heavy synchronous requires block the event loop.
  await sleep(250);
  try {
    ensureWwebjs();
  } catch (err) {
    state.phase = "error";
    state.lastError = `Scanner libraries failed to load: ${err.message}`;
    initInProgress = false;
    throw err;
  }
  await sleep(0);
  ensureWaWebCache();
  resetChromePathCache();
  await sleep(0);
  if (!validateHostedChrome()) {
    initInProgress = false;
    return;
  }

  if (fresh) {
    wipeAuthDir();
  } else if (IS_HOSTED && hasSessionLinked()) {
    const sessionDir = sessionDirPath();
    let sessionUsable = fs.existsSync(sessionDir);
    if (sessionUsable) {
      try {
        const entries = fs.readdirSync(sessionDir).filter((name) => !name.startsWith("."));
        sessionUsable = entries.length > 0;
      } catch {
        sessionUsable = false;
      }
    }
    if (!sessionUsable) {
      console.warn("[WhatsApp] Saved session missing or empty — clearing for fresh QR.");
      wipeAuthDir();
    }
  }

  await destroyClient();
  client = createClient();
  bindClientEvents(client);
  const linked = hasSessionLinked();
  state.phase = linked && !IS_HOSTED ? "restoring" : "starting";
  state.lastError = IS_HOSTED
    ? "Loading WhatsApp scanner — QR will appear shortly."
    : linked
      ? "Restoring saved WhatsApp session — no scan needed if already linked on your phone."
      : null;
  state.ready = false;
  state.authenticatingSince = null;
  state.waState = null;
  state.sessionLinked = linked;
  state.qr = null;
  state.qrGeneration = 0;
  if (linked && !IS_HOSTED) {
    console.log("[WhatsApp] Restoring saved WhatsApp session from disk…");
    scheduleRestoreWatchdog();
  } else if (linked && IS_HOSTED) {
    console.log("[WhatsApp] Checking saved session on server (QR appears if link expired)…");
  }
  scheduleQrStartupWatchdog();
  try {
    await Promise.race([
      client.initialize(),
      sleep(INIT_TIMEOUT_MS).then(() => {
        throw new Error("INIT_TIMEOUT");
      }),
    ]);
  } catch (err) {
    const errText = String(err?.message || err || "");
    const timedOut = errText === "INIT_TIMEOUT" || /timeout/i.test(errText);
    const injectFailed = /inject|ExecutionContext|evaluate/i.test(`${errText}${err?.stack || ""}`);
    if (IS_HOSTED && !_retried && (timedOut || injectFailed || errText)) {
      console.warn("[WhatsApp] Scanner init failed — retrying once with remote WA Web cache…", errText);
      process.env.WHATSAPP_REMOTE_CACHE = "1";
      await destroyClient();
      wipeAuthDir();
      initInProgress = false;
      return initializeClient({ fresh: true, _retried: true });
    }
    state.phase = "error";
    state.lastError = `Scanner failed to start: ${errText}. Click Reset Connection and wait for a fresh QR.`;
    console.error("[WhatsApp] initialize() failed:", errText);
    throw err;
  } finally {
    initInProgress = false;
  }
}

function waitForReady(timeoutMs) {
  return new Promise((resolve) => {
    const started = Date.now();
    const check = () => {
      if (state.ready && client) return resolve(true);
      if (state.phase === "qr" && state.qr) return resolve(false);
      if (Date.now() - started >= timeoutMs) return resolve(false);
      setTimeout(check, 400);
    };
    check();
  });
}

async function softRecoverClient(reason) {
  if (recovering) {
    while (recovering) {
      await sleep(300);
    }
    return state.ready;
  }

  recovering = true;
  const linked = hasSessionLinked();
  state.lastError = linked
    ? "Reconnecting saved WhatsApp session…"
    : "Reconnecting WhatsApp session…";
  console.warn("[WhatsApp] Recovering session:", reason);

  try {
    if (client) {
      try {
        const waState = await client.getState();
        if (waState === "CONNECTED") {
          markReady("recover-check");
          return true;
        }
      } catch {
        /* fall through */
      }
    }

    state.ready = false;
    state.phase = linked ? "restoring" : "reconnecting";
    await destroyClient();
    await initializeClient();
    const ok = await waitForReady(90000);
    if (!ok && state.phase === "qr") {
      state.lastError = linked
        ? "Saved session expired — scan the QR code once to link again."
        : "Scan the QR code in the billing app to connect WhatsApp.";
    } else if (!ok) {
      state.lastError = linked
        ? "WhatsApp reconnect timed out. Keep the scanner running and try sending again."
        : "WhatsApp reconnect timed out. Click Reset Connection if needed.";
    }
    return ok;
  } finally {
    recovering = false;
  }
}

async function resetSession() {
  clearTimeout(scheduleReconnect._timer);
  clearRestoreWatchdog();
  releaseSendLock();
  recovering = false;
  qrDuringRestoreCount = 0;
  state.ready = false;
  state.qr = null;
  state.qrGeneration = 0;
  state.lastError = null;
  state.phase = "starting";
  state.loadingPercent = 0;

  await destroyClient();
  await initializeClient({ fresh: true });
}

function normalizePhone(phone) {
  let digits = String(phone || "").replace(/\D/g, "");
  if (digits.length === 10) digits = "91" + digits;
  if (digits.startsWith("0") && digits.length === 11) digits = "91" + digits.slice(1);
  return digits;
}

function serializeWid(wid) {
  if (!wid) return null;
  if (typeof wid === "string") return wid;
  if (wid._serialized) return wid._serialized;
  if (wid.user && wid.server) return `${wid.user}@${wid.server}`;
  return null;
}

async function ensureChatRegistered(chatId) {
  if (!client?.pupPage || !chatId || chatId.includes("@lid")) return false;
  try {
    return await client.pupPage.evaluate(async (targetChatId) => {
      const widFactory = window.require("WAWebWidFactory");
      const chatWid = widFactory.createWid(targetChatId);
      const chat =
        window.require("WAWebCollections").Chat.get(chatWid) ||
        (await window.require("WAWebFindChatAction").findOrCreateLatestChat(chatWid))?.chat;
      return Boolean(chat);
    }, chatId);
  } catch (err) {
    console.warn("[WhatsApp] ensureChatRegistered:", err.message);
    return false;
  }
}

/** Phone-only chat IDs — avoid @lid lookups that trigger WhatsApp Web getter crashes. */
async function resolveSendTargets(digits) {
  const phoneChatId = `${digits}@c.us`;
  let registeredId = null;

  try {
    const registered = await client.getNumberId(digits);
    registeredId = serializeWid(registered);
  } catch (err) {
    console.warn("[WhatsApp] getNumberId failed:", err.message);
  }

  if (!registeredId) {
    const err = new Error("This phone number is not registered on WhatsApp.");
    err.code = "NOT_ON_WHATSAPP";
    throw err;
  }

  const targets = [];
  if (registeredId !== phoneChatId) targets.push(registeredId);
  targets.push(phoneChatId);
  return targets;
}

const SEND_OPTIONS = { sendSeen: false, sendMediaAsDocument: true };
const SEND_IMAGE_OPTIONS = { sendSeen: false, sendMediaAsDocument: false };

async function quickSendCheck() {
  if (!client || !state.ready) {
    throw new Error("WhatsApp not connected.");
  }
  const waState = await client.getState();
  if (waState !== "CONNECTED") {
    state.ready = false;
    throw new Error(`WhatsApp not fully connected (${waState || "unknown"}).`);
  }
}

async function assertSendReady({ maxCommsWaitMs = 3000 } = {}) {
  await quickSendCheck();
  if (await isWhatsAppStoreReady()) {
    if (await isCommsReady()) return;
  }
  state.ready = false;
  state.phase = "loading";
  state.lastError = "WhatsApp is still connecting. Wait a few seconds and try again.";
  const commsOk = maxCommsWaitMs > 0 ? await waitForCommsReady(maxCommsWaitMs) : false;
  if (!commsOk) {
    const err = new Error("WhatsApp is still starting its send layer. Wait a few seconds, then try again.");
    err.code = "COMMS_NOT_READY";
    throw err;
  }
  state.ready = true;
  state.phase = "ready";
}

async function lookupRegisteredChatId(digits) {
  try {
    const registered = await Promise.race([
      client.getNumberId(digits),
      sleep(GET_NUMBER_ID_TIMEOUT_MS).then(() => null),
    ]);
    return serializeWid(registered);
  } catch (err) {
    console.warn("[WhatsApp] getNumberId failed:", err.message);
    return null;
  }
}

async function sendMediaToChat(chatId, media, caption) {
  await client.sendMessage(chatId, media, {
    ...SEND_OPTIONS,
    caption: caption || "",
  });
}

async function performSend(digits, message, filePath, filename) {
  ensureWwebjs();
  await quickSendCheck();

  const media = MessageMedia.fromFilePath(filePath);
  media.filename = filename || path.basename(filePath);
  const caption = message || "";
  const phoneChatId = `${digits}@c.us`;

  // Fast path — most Indian numbers work with @c.us directly (skip slow getNumberId).
  try {
    await sendMediaToChat(phoneChatId, media, caption);
    return;
  } catch (err) {
    if (err?.code === "NOT_ON_WHATSAPP") throw err;
    console.warn("[WhatsApp] Direct send failed, trying registered id:", err?.message || err);
  }

  const registeredId = await lookupRegisteredChatId(digits);
  if (!registeredId) {
    const err = new Error("This phone number is not registered on WhatsApp.");
    err.code = "NOT_ON_WHATSAPP";
    throw err;
  }

  const targets = registeredId === phoneChatId ? [phoneChatId] : [registeredId, phoneChatId];
  let lastErr = null;
  for (const chatId of targets) {
    for (let attempt = 1; attempt <= 2; attempt += 1) {
      try {
        await sendMediaToChat(chatId, media, caption);
        return;
      } catch (err) {
        lastErr = err;
        if (err?.code === "NOT_ON_WHATSAPP") throw err;
        if (isLidError(err) || isContactGetterError(err)) break;
        if (isCommsError(err) && attempt < 2) {
          await sleep(1500);
          continue;
        }
        throw err;
      }
    }
  }

  throw lastErr || new Error("Failed to send on WhatsApp.");
}

async function performSendText(digits, message) {
  ensureWwebjs();
  await assertSendReady({ maxCommsWaitMs: 8000 });

  const targets = await resolveSendTargets(digits);
  const text = String(message || "").trim();
  if (!text) throw new Error("Message is required.");

  let lastErr = null;
  for (const chatId of targets) {
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        if (attempt > 1) await assertSendReady({ maxCommsWaitMs: 2000 });
        await ensureChatRegistered(chatId);
        await client.sendMessage(chatId, text, { sendSeen: false });
        return;
      } catch (err) {
        lastErr = err;
        if (isLidError(err) || isContactGetterError(err)) {
          console.warn(`[WhatsApp] Contact/LID error on ${chatId} — trying alternate chat id…`);
          break;
        }
        if (isCommsError(err) && attempt < 3) {
          console.warn(`[WhatsApp] Comms not ready (attempt ${attempt}/6) — retrying…`);
          await sleep(4000 * attempt);
          continue;
        }
        throw err;
      }
    }
  }

  throw lastErr || new Error("Failed to send on WhatsApp.");
}

async function performSendImage(digits, message, filePath, filename) {
  ensureWwebjs();
  await assertSendReady({ maxCommsWaitMs: 8000 });

  const targets = await resolveSendTargets(digits);
  const media = MessageMedia.fromFilePath(filePath);
  media.filename = filename || path.basename(filePath);
  const caption = String(message || "").trim();

  let lastErr = null;
  for (const chatId of targets) {
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        if (attempt > 1) await assertSendReady({ maxCommsWaitMs: 2000 });
        await ensureChatRegistered(chatId);
        await client.sendMessage(chatId, media, {
          ...SEND_IMAGE_OPTIONS,
          caption,
        });
        return;
      } catch (err) {
        lastErr = err;
        if (isLidError(err) || isContactGetterError(err)) {
          console.warn(`[WhatsApp] Contact/LID error on ${chatId} — trying alternate chat id…`);
          break;
        }
        if (isCommsError(err) && attempt < 3) {
          console.warn(`[WhatsApp] Comms not ready (attempt ${attempt}/6) — retrying…`);
          await sleep(4000 * attempt);
          continue;
        }
        throw err;
      }
    }
  }

  throw lastErr || new Error("Failed to send image on WhatsApp.");
}

app.get("/health", (_req, res) => {
  clearSendLockIfStale();
  res.json({
    ok: true,
    ready: state.ready,
    phase: state.phase,
    qr: Boolean(state.qr),
    sessionLinked: state.sessionLinked || hasSessionLinked(),
    sendInProgress,
    startupSeconds: Math.max(0, Math.floor((Date.now() - bridgeStartedAt) / 1000)),
  });
});

app.get("/status", (_req, res) => {
  clearSendLockIfStale();
  const authSeconds = state.authenticatingSince
    ? Math.floor((Date.now() - state.authenticatingSince) / 1000)
    : 0;
  const linked = state.sessionLinked || hasSessionLinked();
  const restoring =
    !IS_HOSTED &&
    linked &&
    !state.ready &&
    !state.qr &&
    ["starting", "restoring", "loading", "authenticating", "connecting", "reconnecting"].includes(
      state.phase
    );
  const startupSeconds = Math.max(0, Math.floor((Date.now() - bridgeStartedAt) / 1000));
  res.json({
    ready: state.ready,
    qr: state.qr,
    lastError: state.lastError,
    phase: state.phase,
    loadingPercent: state.loadingPercent,
    recovering,
    waState: state.waState,
    authenticatingSeconds: authSeconds,
    startupSeconds,
    sessionLinked: linked,
    sessionRestoring: restoring,
    qrGeneration: state.qrGeneration,
    sendInProgress,
    sendBusyForSec: sendInProgressSince
      ? Math.max(0, Math.floor((Date.now() - sendInProgressSince) / 1000))
      : 0,
    hosted: IS_HOSTED,
  });
});

app.post("/reset", async (_req, res) => {
  try {
    await resetSession();
    res.json({ ok: true });
  } catch (err) {
    console.error("[WhatsApp] Reset failed:", err);
    res.status(500).json({ error: err.message || "Reset failed." });
  }
});

app.post("/send-image", async (req, res) => {
  if (!acquireSendLock()) {
    return sendBusyResponse(res);
  }

  if (!state.ready || !client) {
    releaseSendLock();
    return res.status(503).json({
      error: "WhatsApp not connected. Scan QR code in billing app.",
      needsReconnect: true,
    });
  }

  const { phone, message, imagePath, filename } = req.body || {};
  const digits = normalizePhone(phone);
  if (digits.length < 11) {
    releaseSendLock();
    return res.status(400).json({ error: "Invalid phone number." });
  }

  const filePath = path.resolve(String(imagePath || ""));
  if (!filePath || !fs.existsSync(filePath)) {
    releaseSendLock();
    return res.status(400).json({ error: "Offer image file not found." });
  }

  try {
    await withSendTimeout(performSendImage(digits, message, filePath, filename), "image send");
    return res.json({ ok: true });
  } catch (err) {
    console.error("[WhatsApp] Send-image failed:", err);
    return res.status(500).json({
      error: err.message || "Failed to send image on WhatsApp.",
      needsReconnect: isSessionError(err),
    });
  } finally {
    releaseSendLock();
  }
});

app.post("/send-text", async (req, res) => {
  if (!acquireSendLock()) {
    return sendBusyResponse(res);
  }

  if (!state.ready || !client) {
    releaseSendLock();
    return res.status(503).json({
      error: "WhatsApp not connected. Scan QR code in billing app.",
      needsReconnect: true,
    });
  }

  const { phone, message } = req.body || {};
  const digits = normalizePhone(phone);
  if (digits.length < 11) {
    releaseSendLock();
    return res.status(400).json({ error: "Invalid phone number." });
  }
  if (!String(message || "").trim()) {
    releaseSendLock();
    return res.status(400).json({ error: "Message is required." });
  }

  try {
    try {
      await withSendTimeout(performSendText(digits, message), "text send");
      return res.json({ ok: true });
    } catch (err) {
      if (err.code === "NOT_ON_WHATSAPP") {
        return res.status(400).json({ error: err.message });
      }

      if (isSessionError(err)) {
        console.error("[WhatsApp] Send-text session error:", err.message);
        releaseSendLock();
        const reconnected = await softRecoverClient(err.message);
        if (!reconnected) {
          return res.status(503).json({
            error: "WhatsApp send layer not ready. Open WhatsApp in the billing app, wait until Ready, then try again.",
            needsReconnect: true,
          });
        }
        if (!acquireSendLock()) {
          return sendBusyResponse(res);
        }
        try {
          await withSendTimeout(performSendText(digits, message), "text send retry");
          return res.json({ ok: true, recovered: true });
        } finally {
          releaseSendLock();
        }
      }

      throw err;
    }
  } catch (err) {
    console.error("[WhatsApp] Send-text failed:", err);
    return res.status(500).json({ error: err.message || "Send failed." });
  } finally {
    releaseSendLock();
  }
});

app.post("/send", async (req, res) => {
  if (!acquireSendLock()) {
    return sendBusyResponse(res);
  }

  if (!state.ready || !client) {
    releaseSendLock();
    return res.status(503).json({
      error: "WhatsApp not connected. Scan QR code in billing app.",
      needsReconnect: true,
    });
  }

  const { phone, message, pdfPath, filename } = req.body || {};
  const digits = normalizePhone(phone);
  if (digits.length < 11) {
    releaseSendLock();
    return res.status(400).json({ error: "Invalid phone number." });
  }

  const filePath = path.resolve(String(pdfPath || ""));
  if (!filePath || !fs.existsSync(filePath)) {
    releaseSendLock();
    return res.status(400).json({ error: "Invoice PDF file not found." });
  }

  try {
    try {
      await withSendTimeout(performSend(digits, message, filePath, filename), "bill send");
      return res.json({ ok: true });
    } catch (err) {
      if (err.code === "NOT_ON_WHATSAPP") {
        return res.status(400).json({ error: err.message });
      }

      if (isSessionError(err)) {
        state.ready = false;
        console.error("[WhatsApp] Send session error:", err.message);
        const friendly = isCommsError(err)
          ? "WhatsApp is still connecting — wait 10 seconds and tap Send again."
          : isStoreError(err)
            ? "WhatsApp is still loading — wait 10 seconds and try again."
            : "WhatsApp session hiccup — wait 10 seconds and tap Send again.";
        return res.status(503).json({
          error: friendly,
          needsReconnect: true,
        });
      }

      if (isStoreError(err)) {
        return res.status(503).json({
          error:
            "WhatsApp is still loading its chat system. Wait about 1 minute, then try Send on WhatsApp again.",
          needsReconnect: true,
        });
      }

      if (isCommsError(err)) {
        return res.status(503).json({
          error:
            "WhatsApp is still connecting on the server. Wait about 1 minute, then try Send on WhatsApp again.",
          needsReconnect: true,
        });
      }

      if (isLidError(err)) {
        return res.status(500).json({
          error:
            "WhatsApp could not open a chat for this number. Click Reset Connection in WhatsApp settings, scan QR again, then retry.",
          needsReconnect: true,
        });
      }

      throw err;
    }
  } catch (err) {
    console.error("[WhatsApp] Send failed:", err);
    return res.status(500).json({
      error: err.message || "Failed to send on WhatsApp.",
      needsReconnect: isSessionError(err),
    });
  } finally {
    releaseSendLock();
  }
});

const server = app.listen(PORT, "127.0.0.1", () => {
  acquireSingleInstanceLock();
  process.on("SIGINT", () => {
    releaseSingleInstanceLock();
    process.exit(0);
  });
  process.on("SIGTERM", () => {
    releaseSingleInstanceLock();
    process.exit(0);
  });
  process.on("exit", releaseSingleInstanceLock);

  ensureAuthDirs();
  state.phase = "booting";
  console.log(`[WhatsApp] Bridge HTTP ready on http://127.0.0.1:${PORT}`);
  console.log(`[WhatsApp] Session data: ${AUTH_DIR}`);
  console.log(`[WhatsApp] WA Web version: ${WA_WEB_VERSION}`);
  setImmediate(() => {
    migrateLegacyAuthDir();
    state.sessionLinked = hasSessionLinked();
    initializeClient().catch((err) => {
      state.phase = "error";
      state.lastError = err.message;
      console.error("[WhatsApp] Init failed:", err.message);
    });
  });
});

server.on("error", (err) => {
  if (err.code === "EADDRINUSE") {
    console.error(`[WhatsApp] Port ${PORT} is already in use. Close the other WhatsApp Scanner window, or run Reset WhatsApp.bat`);
    process.exit(1);
  }
  throw err;
});
