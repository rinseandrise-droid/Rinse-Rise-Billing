/**
 * Lightweight WhatsApp bridge powered by @whiskeysockets/baileys (no Chromium).
 * Exposes the same HTTP API on localhost for the Flask billing backend.
 */
const express = require("express");
const fs = require("fs");
const path = require("path");
const pino = require("pino");
const QRCode = require("qrcode");

const app = express();
app.use(express.json({ limit: "25mb" }));

const PORT = Number(process.env.WHATSAPP_BRIDGE_PORT || 3001);
const DATA_DIR = path.resolve(
  process.env.DATA_DIR || path.join(__dirname, "..", "data")
);
const AUTH_DIR = path.resolve(
  process.env.WHATSAPP_AUTH_DIR || path.join(DATA_DIR, "whatsapp-auth")
);
const CLIENT_ID = process.env.WHATSAPP_CLIENT_ID || "rinse-rise";
const BAILEYS_AUTH_DIR = path.join(AUTH_DIR, `baileys-${CLIENT_ID}`);
const LOCK_FILE = path.join(AUTH_DIR, ".bridge.lock");
const IS_HOSTED = Boolean(process.env.RAILWAY_ENVIRONMENT);
const SEND_TIMEOUT_MS = Number(process.env.WHATSAPP_SEND_TIMEOUT_MS || 45000);

const state = {
  ready: false,
  qr: null,
  lastError: null,
  phase: "booting",
  loadingPercent: 0,
  waState: null,
  authenticatingSince: null,
  sessionLinked: false,
  qrGeneration: 0,
};

let sock = null;
let baileysMod = null;
let bridgeStartedAt = Date.now();
let initInProgress = false;
let reconnectTimer = null;
let sendInProgress = false;
let sendInProgressSince = 0;
let sendQueue = Promise.resolve();
let socketGeneration = 0;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function ensureAuthDir() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  fs.mkdirSync(BAILEYS_AUTH_DIR, { recursive: true });
}

function hasSessionLinked() {
  try {
    const credsPath = path.join(BAILEYS_AUTH_DIR, "creds.json");
    if (!fs.existsSync(credsPath)) return false;
    const raw = fs.readFileSync(credsPath, "utf8");
    const creds = JSON.parse(raw);
    return Boolean(creds?.me?.id || creds?.registered);
  } catch {
    return false;
  }
}

function wipeBaileysAuth() {
  try {
    fs.rmSync(BAILEYS_AUTH_DIR, {
      recursive: true,
      force: true,
      maxRetries: 3,
      retryDelay: 150,
    });
  } catch (err) {
    console.warn("[WhatsApp] wipeBaileysAuth:", err.message);
  }
  fs.mkdirSync(BAILEYS_AUTH_DIR, { recursive: true });
  state.sessionLinked = false;
}

function acquireSingleInstanceLock() {
  ensureAuthDir();
  try {
    if (fs.existsSync(LOCK_FILE)) {
      const oldPid = Number(fs.readFileSync(LOCK_FILE, "utf8").trim());
      if (oldPid && oldPid !== process.pid) {
        try {
          process.kill(oldPid, 0);
        } catch {
          fs.rmSync(LOCK_FILE, { force: true });
        }
      }
    }
    fs.writeFileSync(LOCK_FILE, String(process.pid), "utf8");
  } catch {
    /* best-effort lock */
  }
}

function releaseSingleInstanceLock() {
  try {
    if (fs.existsSync(LOCK_FILE)) {
      const pid = Number(fs.readFileSync(LOCK_FILE, "utf8").trim());
      if (pid === process.pid) fs.rmSync(LOCK_FILE, { force: true });
    }
  } catch {
    /* ignore */
  }
}

function normalizePhone(phone) {
  let digits = String(phone || "").replace(/\D/g, "");
  if (digits.length === 10) digits = "91" + digits;
  if (digits.startsWith("0") && digits.length === 11) digits = "91" + digits.slice(1);
  return digits;
}

function clearSendLockIfStale() {
  if (sendInProgress && sendInProgressSince && Date.now() - sendInProgressSince > 60000) {
    sendInProgress = false;
    sendInProgressSince = 0;
  }
}

function withSendLock(fn) {
  const run = async () => {
    sendInProgress = true;
    sendInProgressSince = Date.now();
    try {
      return await fn();
    } finally {
      sendInProgress = false;
      sendInProgressSince = 0;
    }
  };
  const next = sendQueue.then(run, run);
  sendQueue = next.catch(() => {});
  return next;
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    sleep(ms).then(() => {
      const err = new Error(`${label} timed out after ${Math.round(ms / 1000)}s`);
      err.code = "TIMEOUT";
      throw err;
    }),
  ]);
}

async function getBaileys() {
  if (!baileysMod) {
    baileysMod = await import("@whiskeysockets/baileys");
  }
  return baileysMod;
}

function closeCurrentSocket() {
  if (!sock) return;
  try {
    sock.ev.removeAllListeners();
    sock.end(undefined);
  } catch {
    /* ignore */
  }
  sock = null;
}

function scheduleReconnect(delayMs = 2000) {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    void initializeClient({ fresh: false }).catch((err) => {
      console.error("[WhatsApp] Reconnect failed:", err.message);
    });
  }, delayMs);
}

async function initializeClient({ fresh = false } = {}) {
  if (initInProgress) return;
  initInProgress = true;
  clearTimeout(reconnectTimer);
  const myGen = ++socketGeneration;

  try {
    ensureAuthDir();
    closeCurrentSocket();

    if (fresh) {
      wipeBaileysAuth();
    }

    const linked = hasSessionLinked();
    state.ready = false;
    state.qr = null;
    if (fresh) state.qrGeneration = 0;
    state.sessionLinked = linked;
    state.phase = linked ? "restoring" : "starting";
    state.lastError = linked
      ? "Restoring saved WhatsApp session — no scan needed if already linked."
      : "Starting WhatsApp connection — QR code will appear in a few seconds.";
    state.waState = null;

    const {
      default: makeWASocket,
      useMultiFileAuthState,
      DisconnectReason,
      fetchLatestBaileysVersion,
      Browsers,
    } = await getBaileys();

    const { state: authState, saveCreds } = await useMultiFileAuthState(BAILEYS_AUTH_DIR);

    let version;
    try {
      const latest = await withTimeout(fetchLatestBaileysVersion(), 5000, "WA version lookup");
      version = latest?.version;
    } catch {
      version = undefined;
    }

    if (myGen !== socketGeneration) return;

    const waSock = makeWASocket({
      auth: authState,
      version,
      logger: pino({ level: "silent" }),
      browser: Browsers.ubuntu("Chrome"),
      printQRInTerminal: false,
      syncFullHistory: false,
      markOnlineOnConnect: false,
      connectTimeoutMs: 30000,
    });

    sock = waSock;

    waSock.ev.on("creds.update", saveCreds);

    waSock.ev.on("connection.update", async (update) => {
      if (myGen !== socketGeneration) return;
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        try {
          state.qr = await QRCode.toDataURL(qr, {
            margin: 2,
            width: 360,
            errorCorrectionLevel: "H",
          });
          state.qrGeneration += 1;
          state.phase = "qr";
          state.ready = false;
          state.sessionLinked = false;
          state.lastError = null;
          console.log(`[WhatsApp] QR ready (#${state.qrGeneration}) — scan in billing app.`);
        } catch (err) {
          state.lastError = "Could not render QR code.";
          console.error("[WhatsApp] QR render error:", err.message);
        }
      }

      if (connection === "connecting") {
        if (state.phase !== "qr") {
          state.phase = state.sessionLinked ? "restoring" : "connecting";
        }
      }

      if (connection === "open") {
        state.ready = true;
        state.phase = "ready";
        state.qr = null;
        state.lastError = null;
        state.sessionLinked = true;
        state.authenticatingSince = null;
        state.waState = "CONNECTED";
        console.log("[WhatsApp] Connected and ready to send invoices (Baileys).");
      }

      if (connection === "close") {
        state.ready = false;
        state.waState = "DISCONNECTED";
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const reasonMsg = lastDisconnect?.error?.message || "Connection closed";
        console.warn(`[WhatsApp] Connection closed (${statusCode || "unknown"}): ${reasonMsg}`);

        if (
          statusCode === DisconnectReason.loggedOut ||
          statusCode === 401 ||
          statusCode === DisconnectReason.badSession
        ) {
          console.warn("[WhatsApp] Session logged out or invalid — clearing for fresh QR.");
          wipeBaileysAuth();
          state.phase = "starting";
          state.lastError = "WhatsApp session expired — generating a new QR code…";
          scheduleReconnect(500);
          return;
        }

        const delay = statusCode === DisconnectReason.restartRequired ? 400 : 2000;
        state.phase = state.sessionLinked ? "restoring" : "starting";
        state.lastError = "Reconnecting to WhatsApp…";
        scheduleReconnect(delay);
      }
    });
  } catch (err) {
    state.phase = "error";
    state.lastError = `WhatsApp failed to start: ${err.message}`;
    console.error("[WhatsApp] initializeClient error:", err);
  } finally {
    initInProgress = false;
  }
}

async function resetSession() {
  clearTimeout(reconnectTimer);
  state.ready = false;
  state.qr = null;
  state.qrGeneration = 0;
  state.lastError = null;
  state.phase = "starting";
  state.loadingPercent = 0;
  await initializeClient({ fresh: true });
}

async function resolveTargetJid(digits) {
  if (!sock) throw new Error("WhatsApp not connected.");
  try {
    const results = await withTimeout(sock.onWhatsApp(digits), 5000, "onWhatsApp");
    if (Array.isArray(results) && results.length > 0) {
      const entry = results[0];
      if (entry && entry.exists === false) {
        const err = new Error(
          `Could not deliver to ${digits.slice(-10)}. This number is not registered on WhatsApp.`
        );
        err.code = "NOT_ON_WHATSAPP";
        throw err;
      }
      if (entry?.jid) return entry.jid;
    }
  } catch (err) {
    if (err.code === "NOT_ON_WHATSAPP") throw err;
    console.warn("[WhatsApp] onWhatsApp check skipped:", err.message);
  }
  return `${digits}@s.whatsapp.net`;
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
  const linked = state.sessionLinked || hasSessionLinked();
  const restoring =
    linked &&
    !state.ready &&
    !state.qr &&
    ["starting", "restoring", "connecting"].includes(state.phase);
  const startupSeconds = Math.max(0, Math.floor((Date.now() - bridgeStartedAt) / 1000));
  res.json({
    ready: state.ready,
    qr: state.qr,
    lastError: state.lastError,
    phase: state.phase,
    loadingPercent: state.loadingPercent,
    recovering: false,
    waState: state.waState,
    authenticatingSeconds: 0,
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

app.post("/send", async (req, res) => {
  try {
    const result = await withSendLock(async () => {
      if (!state.ready || !sock) {
        return {
          status: 503,
          body: {
            error: "WhatsApp not connected. Scan QR code in billing app.",
            needsReconnect: true,
          },
        };
      }

      const { phone, message, pdfPath, filename } = req.body || {};
      const digits = normalizePhone(phone);
      if (digits.length < 11) {
        return { status: 400, body: { error: "Invalid phone number." } };
      }

      const filePath = path.resolve(String(pdfPath || ""));
      if (!filePath || !fs.existsSync(filePath)) {
        return { status: 400, body: { error: "Invoice PDF file not found." } };
      }

      const jid = await resolveTargetJid(digits);
      const pdfBuffer = fs.readFileSync(filePath);
      const docName = filename || path.basename(filePath);
      const caption = String(message || "").trim() || "Your invoice from Rinse & Rise Laundryrite is attached.";

      await withTimeout(
        sock.sendMessage(jid, {
          document: pdfBuffer,
          mimetype: "application/pdf",
          fileName: docName,
          caption,
        }),
        SEND_TIMEOUT_MS,
        "PDF send"
      );

      console.log(`[WhatsApp] Bill PDF sent to ${digits.slice(-10)} (${docName})`);
      return { status: 200, body: { ok: true, verified: true } };
    });

    return res.status(result.status).json(result.body);
  } catch (err) {
    if (err.code === "NOT_ON_WHATSAPP") {
      return res.status(400).json({ error: err.message });
    }
    console.error("[WhatsApp] Send failed:", err.message);
    return res.status(500).json({
      error: err.message || "Failed to send on WhatsApp.",
      needsReconnect: !state.ready,
    });
  }
});

app.post("/send-image", async (req, res) => {
  try {
    const result = await withSendLock(async () => {
      if (!state.ready || !sock) {
        return {
          status: 503,
          body: {
            error: "WhatsApp not connected. Scan QR code in billing app.",
            needsReconnect: true,
          },
        };
      }

      const { phone, message, imagePath } = req.body || {};
      const digits = normalizePhone(phone);
      if (digits.length < 11) {
        return { status: 400, body: { error: "Invalid phone number." } };
      }

      const filePath = path.resolve(String(imagePath || ""));
      if (!filePath || !fs.existsSync(filePath)) {
        return { status: 400, body: { error: "Offer image file not found." } };
      }

      const jid = await resolveTargetJid(digits);
      const imgBuffer = fs.readFileSync(filePath);
      await withTimeout(
        sock.sendMessage(jid, {
          image: imgBuffer,
          caption: String(message || ""),
        }),
        SEND_TIMEOUT_MS,
        "Image send"
      );

      return { status: 200, body: { ok: true } };
    });

    return res.status(result.status).json(result.body);
  } catch (err) {
    if (err.code === "NOT_ON_WHATSAPP") {
      return res.status(400).json({ error: err.message });
    }
    console.error("[WhatsApp] Image send failed:", err.message);
    return res.status(500).json({ error: err.message || "Failed to send image on WhatsApp." });
  }
});

app.post("/send-text", async (req, res) => {
  try {
    const result = await withSendLock(async () => {
      if (!state.ready || !sock) {
        return {
          status: 503,
          body: {
            error: "WhatsApp not connected. Scan QR code in billing app.",
            needsReconnect: true,
          },
        };
      }

      const { phone, message } = req.body || {};
      const digits = normalizePhone(phone);
      if (digits.length < 11) {
        return { status: 400, body: { error: "Invalid phone number." } };
      }

      const text = String(message || "").trim();
      if (!text) {
        return { status: 400, body: { error: "Message text is required." } };
      }

      const jid = await resolveTargetJid(digits);
      await withTimeout(
        sock.sendMessage(jid, { text }),
        SEND_TIMEOUT_MS,
        "Text send"
      );

      return { status: 200, body: { ok: true } };
    });

    return res.status(result.status).json(result.body);
  } catch (err) {
    if (err.code === "NOT_ON_WHATSAPP") {
      return res.status(400).json({ error: err.message });
    }
    console.error("[WhatsApp] Text send failed:", err.message);
    return res.status(500).json({ error: err.message || "Failed to send message on WhatsApp." });
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

  bridgeStartedAt = Date.now();
  console.log(`[WhatsApp] Baileys bridge HTTP ready on http://127.0.0.1:${PORT}`);
  void initializeClient({ fresh: false });
});

server.on("error", (err) => {
  if (err.code === "EADDRINUSE") {
    console.error(`[WhatsApp] Port ${PORT} is already in use.`);
  } else {
    console.error("[WhatsApp] Server error:", err.message);
  }
  process.exit(1);
});
