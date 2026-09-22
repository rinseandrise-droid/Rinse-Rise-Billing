/**
 * Instant health proxy for Railway — listens on the public bridge port (3001)
 * and forwards to the real WhatsApp bridge (3002). While the scanner boots,
 * /health and /status respond immediately so the billing UI can show progress.
 */
const http = require("http");

const PUBLIC_PORT = Number(process.env.WHATSAPP_BRIDGE_PORT || 3001);
const INTERNAL_PORT = Number(process.env.WHATSAPP_BRIDGE_INTERNAL_PORT || 3002);
const INTERNAL_HOST = "127.0.0.1";
const PROXY_TIMEOUT_MS = Number(process.env.WHATSAPP_PROXY_TIMEOUT_MS || 8000);

const startedAt = Date.now();

function bootingHealth() {
  const startupSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  return JSON.stringify({
    ok: true,
    ready: false,
    phase: "booting",
    qr: false,
    sessionLinked: false,
    sendInProgress: false,
    startupSeconds,
  });
}

function bootingStatus() {
  const startupSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  return JSON.stringify({
    ready: false,
    qr: null,
    lastError: "Loading WhatsApp scanner — QR will appear shortly.",
    phase: "booting",
    loadingPercent: Math.min(85, 10 + startupSeconds * 3),
    recovering: false,
    waState: null,
    authenticatingSeconds: 0,
    startupSeconds,
    sessionLinked: false,
    sessionRestoring: false,
    qrGeneration: 0,
    sendInProgress: false,
    sendBusyForSec: 0,
    hosted: true,
  });
}

function sendBooting(req, res) {
  const path = String(req.url || "").split("?")[0];
  const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  let body;
  if (path === "/status" && elapsed >= 90 && !internalBridgeUp) {
    body = JSON.stringify({
      ready: false,
      qr: null,
      lastError:
        "Scanner crashed while loading — click Reset Connection, wait 20 seconds, then scan the new QR.",
      phase: "error",
      loadingPercent: 0,
      recovering: false,
      waState: null,
      authenticatingSeconds: 0,
      startupSeconds: elapsed,
      sessionLinked: false,
      sessionRestoring: false,
      qrGeneration: 0,
      sendInProgress: false,
      sendBusyForSec: 0,
      hosted: true,
    });
  } else {
    body = path === "/status" ? bootingStatus() : bootingHealth();
  }
  res.writeHead(200, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

let internalBridgeUp = false;
setInterval(() => {
  const req = http.request(
    {
      hostname: INTERNAL_HOST,
      port: INTERNAL_PORT,
      path: "/health",
      method: "GET",
      timeout: 2000,
    },
    (res) => {
      internalBridgeUp = res.statusCode === 200;
      res.resume();
    }
  );
  req.on("timeout", () => {
    req.destroy();
    internalBridgeUp = false;
  });
  req.on("error", () => {
    internalBridgeUp = false;
  });
  req.end();
}, 4000);

function forward(req, res) {
  const chunks = [];
  req.on("data", (chunk) => chunks.push(chunk));
  req.on("end", () => {
    const body = Buffer.concat(chunks);
    const headers = { ...req.headers };
    if (body.length) {
      headers["content-length"] = String(body.length);
    } else {
      delete headers["content-length"];
    }

    const proxyReq = http.request(
      {
        hostname: INTERNAL_HOST,
        port: INTERNAL_PORT,
        path: req.url,
        method: req.method,
        headers,
        timeout: PROXY_TIMEOUT_MS,
      },
      (proxyRes) => {
        res.writeHead(proxyRes.statusCode || 502, proxyRes.headers);
        proxyRes.pipe(res);
      }
    );

    proxyReq.on("timeout", () => {
      proxyReq.destroy();
      if (req.method === "GET" && (req.url.startsWith("/health") || req.url.startsWith("/status"))) {
        sendBooting(req, res);
        return;
      }
      res.writeHead(503, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "WhatsApp scanner is still starting — try again shortly." }));
    });

    proxyReq.on("error", () => {
      if (req.method === "GET" && (req.url.startsWith("/health") || req.url.startsWith("/status"))) {
        sendBooting(req, res);
        return;
      }
      res.writeHead(503, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "WhatsApp scanner is not ready yet." }));
    });

    if (body.length) proxyReq.write(body);
    proxyReq.end();
  });
}

const server = http.createServer(forward);

server.listen(PUBLIC_PORT, "127.0.0.1", () => {
  console.log(`[WhatsApp Proxy] ${PUBLIC_PORT} -> ${INTERNAL_HOST}:${INTERNAL_PORT}`);
});

server.on("error", (err) => {
  console.error("[WhatsApp Proxy] Failed to start:", err.message);
  process.exit(1);
});
