"""Send invoice PDFs via local WhatsApp bridge service."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows local dev
    fcntl = None  # type: ignore[assignment]

from invoice_pdf import build_whatsapp_message, generate_invoice_pdf, invoice_filename

ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = ROOT / "whatsapp-bridge"
BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:3001").rstrip("/")
BRIDGE_TIMEOUT = 60
BRIDGE_SEND_TIMEOUT = int(os.environ.get("WHATSAPP_SEND_TIMEOUT", "150"))
BRIDGE_PUBLIC_PORT = os.environ.get("WHATSAPP_BRIDGE_PORT", "3001")
BRIDGE_INTERNAL_PORT = os.environ.get("WHATSAPP_BRIDGE_INTERNAL_PORT", "3002")
_spawn_lock = threading.Lock()


def _bridge_status_timeout() -> int:
    return 20 if is_cloud_deployment() else 4


def is_cloud_deployment() -> bool:
    return bool(os.environ.get("RAILWAY_ENVIRONMENT"))


def whatsapp_enabled() -> bool:
    return os.environ.get("WHATSAPP_ENABLED", "1") not in ("0", "false", "False", "no")


def _bridge_health_at(base_url: str, *, timeout: int = 2) -> bool:
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def bridge_is_running(*, timeout: int | None = None) -> bool:
    if timeout is None:
        timeout = 5 if is_cloud_deployment() else 2
    return _bridge_health_at(BRIDGE_URL, timeout=timeout)


def internal_bridge_is_running(*, timeout: int = 2) -> bool:
    return _bridge_health_at(f"http://127.0.0.1:{BRIDGE_INTERNAL_PORT}", timeout=timeout)


def _node_executable() -> str:
    for candidate in (shutil.which("node"), "/usr/bin/node", "/usr/local/bin/node"):
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def _spawn_node_process(script: Path, *, env: dict[str, str], log_name: str) -> bool:
    node = _node_executable()
    if not node or not script.is_file():
        return False
    from paths import data_dir

    log_dir = data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_name
    try:
        log_file = open(log_path, "a", encoding="utf-8")
        subprocess.Popen(
            [node, script.name],
            cwd=str(BRIDGE_DIR),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def _hosted_bridge_env() -> dict[str, str]:
    from paths import data_dir, whatsapp_auth_dir, whatsapp_cache_dir

    env_base = os.environ.copy()
    env_base.setdefault("RAILWAY_ENVIRONMENT", "1")
    env_base.setdefault("NODE_OPTIONS", "--max-old-space-size=1024")
    env_base.setdefault("WHATSAPP_AUTH_DIR", str(whatsapp_auth_dir()))
    env_base.setdefault("WHATSAPP_CACHE_DIR", str(whatsapp_cache_dir()))
    env_base.setdefault("DATA_DIR", str(data_dir()))
    env_base.setdefault("PUPPETEER_EXECUTABLE_PATH", "/usr/bin/chromium")
    env_base.setdefault("PUPPETEER_SKIP_CHROMIUM_DOWNLOAD", "true")
    env_base.setdefault("WHATSAPP_REMOTE_CACHE", "0")
    return env_base


def _spawn_hosted_bridge_processes(*, include_proxy: bool, include_internal: bool) -> None:
    env_base = _hosted_bridge_env()
    proxy_script = BRIDGE_DIR / "proxy.js"
    server_script = BRIDGE_DIR / "server.js"
    node_modules = BRIDGE_DIR / "node_modules"
    if not node_modules.is_dir():
        return
    if include_proxy and proxy_script.is_file():
        proxy_env = {
            **env_base,
            "WHATSAPP_BRIDGE_PORT": BRIDGE_PUBLIC_PORT,
            "WHATSAPP_BRIDGE_INTERNAL_PORT": BRIDGE_INTERNAL_PORT,
        }
        _spawn_node_process(proxy_script, env=proxy_env, log_name="whatsapp-proxy.log")
        time.sleep(0.5)
    if include_internal and server_script.is_file():
        bridge_env = {**env_base, "WHATSAPP_BRIDGE_PORT": BRIDGE_INTERNAL_PORT}
        _spawn_node_process(server_script, env=bridge_env, log_name="whatsapp-bridge.log")


def _ensure_internal_bridge_running() -> None:
    """Restart the real scanner when the health proxy is up but port 3002 crashed."""
    if not is_cloud_deployment() or not whatsapp_enabled():
        return
    if not bridge_is_running(timeout=1) or internal_bridge_is_running(timeout=1):
        return

    from paths import data_dir

    cooldown_marker = data_dir() / ".bridge-internal-respawn"
    now = time.time()
    try:
        if cooldown_marker.is_file() and now - float(cooldown_marker.read_text(encoding="utf-8")) < 25:
            return
    except ValueError:
        pass
    cooldown_marker.write_text(str(now), encoding="utf-8")
    _spawn_hosted_bridge_processes(include_proxy=False, include_internal=True)


def ensure_hosted_whatsapp_stack(*, wait_seconds: float = 0) -> bool:
    """Start proxy + bridge inside the Railway container when entrypoint background jobs did not."""
    if not is_cloud_deployment() or not whatsapp_enabled():
        return bridge_is_running()

    with _spawn_lock:
        from paths import data_dir

        stack_lock = data_dir() / ".bridge-stack.lock"
        stack_lock.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = None
        try:
            lock_handle = open(stack_lock, "a+", encoding="utf-8")
            if fcntl is not None:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    if not bridge_is_running(timeout=1):
                        _spawn_hosted_bridge_processes(include_proxy=True, include_internal=True)
                    elif not internal_bridge_is_running(timeout=1):
                        _ensure_internal_bridge_running()
                    lock_handle.write(f"started={time.time()}\n")
                    lock_handle.flush()
            else:
                if not bridge_is_running(timeout=1):
                    _spawn_hosted_bridge_processes(include_proxy=True, include_internal=True)
                elif not internal_bridge_is_running(timeout=1):
                    _ensure_internal_bridge_running()
        finally:
            if lock_handle is not None and fcntl is not None:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                lock_handle.close()

    _ensure_internal_bridge_running()

    if wait_seconds <= 0:
        return internal_bridge_is_running(timeout=2)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        _ensure_internal_bridge_running()
        if internal_bridge_is_running(timeout=2):
            return True
        time.sleep(1)
    return internal_bridge_is_running(timeout=2)


def bridge_health_snapshot(*, timeout: int = 2) -> dict[str, Any]:
    """Fast WhatsApp snapshot for /api/health — must not block page load."""
    if not whatsapp_enabled():
        return {"available": False, "ready": False, "phase": "disabled"}
    try:
        health = _bridge_request("/health", timeout=timeout)
        return {
            "available": True,
            "ready": bool(health.get("ready")),
            "phase": health.get("phase") or "starting",
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {"available": False, "ready": False, "phase": "offline"}


def try_start_bridge(*, wait_seconds: float = 0) -> bool:
    """Start the Node WhatsApp bridge if installed and not already running."""
    if not whatsapp_enabled():
        return False
    if bridge_is_running():
        return True

    if is_cloud_deployment():
        return ensure_hosted_whatsapp_stack(wait_seconds=wait_seconds)

    node_exe = shutil.which("node")
    server_js = BRIDGE_DIR / "server.js"
    node_modules = BRIDGE_DIR / "node_modules"
    if not node_exe or not server_js.is_file() or not node_modules.is_dir():
        return False

    try:
        from paths import whatsapp_auth_dir, whatsapp_cache_dir

        log_path = BRIDGE_DIR / "bridge.log"
        log_file = open(log_path, "a", encoding="utf-8")
        env = os.environ.copy()
        env.setdefault("WHATSAPP_BRIDGE_PORT", "3001")
        env.setdefault("WHATSAPP_AUTH_DIR", str(whatsapp_auth_dir()))
        env.setdefault("WHATSAPP_CACHE_DIR", str(whatsapp_cache_dir()))
        if is_cloud_deployment():
            env.setdefault("PUPPETEER_EXECUTABLE_PATH", "/usr/bin/chromium")
        subprocess.Popen(
            [node_exe, "server.js"],
            cwd=str(BRIDGE_DIR),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False

    if wait_seconds <= 0:
        return True

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if bridge_is_running():
            return True
        time.sleep(1)
    return bridge_is_running()


def normalize_whatsapp_phone(phone: str) -> str:
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    if len(digits) == 10:
        return "91" + digits
    if digits.startswith("0") and len(digits) == 11:
        return "91" + digits[1:]
    return digits


def _bridge_request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    *,
    timeout: int | None = None,
) -> dict[str, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout or BRIDGE_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_bridge_status(*, auto_start: bool = False) -> dict[str, Any]:
    hosted = is_cloud_deployment()
    if not whatsapp_enabled():
        return {
            "available": False,
            "ready": False,
            "qr": None,
            "lastError": "WhatsApp is disabled on this server.",
            "hosted": hosted,
            "enabled": False,
        }

    if hosted:
        ensure_hosted_whatsapp_stack(wait_seconds=8 if auto_start else 0)
    elif auto_start and not bridge_is_running():
        try_start_bridge(wait_seconds=10)

    if hosted and bridge_is_running(timeout=2) and not internal_bridge_is_running(timeout=2):
        log_tail = read_bridge_log_tail()
        return {
            "available": True,
            "ready": False,
            "qr": None,
            "lastError": (
                "Scanner restarted after a crash — QR should appear within 30–60 seconds."
                if "ExecutionContext" in log_tail or "Client.inject" in log_tail
                else "Scanner is restarting on the server — QR will appear shortly."
            ),
            "phase": "starting",
            "loadingPercent": 20,
            "sessionLinked": False,
            "sessionRestoring": False,
            "sessionLocked": False,
            "qrGeneration": 0,
            "startupSeconds": 0,
            "hosted": hosted,
            "enabled": True,
        }

    try:
        status = _bridge_request("/status", timeout=_bridge_status_timeout())
        return {
            "available": True,
            "ready": bool(status.get("ready")),
            "qr": status.get("qr"),
            "lastError": status.get("lastError"),
            "phase": status.get("phase"),
            "loadingPercent": status.get("loadingPercent"),
            "waState": status.get("waState"),
            "authenticatingSeconds": status.get("authenticatingSeconds"),
            "sessionLinked": bool(status.get("sessionLinked")),
            "sessionRestoring": bool(status.get("sessionRestoring")),
            "sessionLocked": bool(status.get("sessionLocked")),
            "qrGeneration": status.get("qrGeneration", 0),
            "startupSeconds": status.get("startupSeconds", 0),
            "hosted": hosted,
            "enabled": True,
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        if bridge_is_running():
            try:
                health = _bridge_request("/health", timeout=3)
                phase = health.get("phase") or "starting"
                return {
                    "available": True,
                    "ready": bool(health.get("ready")),
                    "qr": None,
                    "lastError": "Loading WhatsApp scanner — QR will appear shortly.",
                    "phase": phase,
                    "loadingPercent": 15 if phase in ("booting", "starting") else 25,
                    "sessionLinked": bool(health.get("sessionLinked")),
                    "sessionRestoring": False,
                    "sessionLocked": False,
                    "qrGeneration": 0,
                    "startupSeconds": health.get("startupSeconds", 0),
                    "hosted": hosted,
                    "enabled": True,
                }
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                pass

        from paths import whatsapp_auth_dir

        auth_dir = whatsapp_auth_dir()
        session_linked = (auth_dir / ".session-linked").is_file()
        log_tail = read_bridge_log_tail() if hosted else ""
        proxy_tail = read_proxy_log_tail() if hosted else ""
        combined_log = "\n".join(part for part in (proxy_tail, log_tail) if part)
        bridge_hint = "Starting WhatsApp scanner — QR will appear in a few seconds."
        if "EADDRINUSE" in combined_log:
            bridge_hint = "Scanner port busy — click Reset Connection, wait 20 seconds, then retry."
        elif "initialize() failed" in combined_log or "Init failed" in combined_log:
            bridge_hint = "Scanner failed to start — click Reset Connection, then scan the QR."
        elif "Chrome binary missing" in combined_log or "could not find Chrome" in combined_log:
            bridge_hint = "Scanner could not find Chrome — redeploy the billing app on Railway."
        elif "Cannot find module" in combined_log:
            bridge_hint = "Scanner dependencies missing on server — redeploy the billing app on Railway."
        elif hosted and not _node_executable():
            bridge_hint = "Node.js missing in container — redeploy the billing app on Railway."
        return {
            "available": False,
            "ready": False,
            "qr": None,
            "lastError": bridge_hint if hosted else None,
            "phase": "starting",
            "sessionLinked": session_linked,
            "sessionRestoring": False if hosted else session_linked,
            "sessionLocked": session_linked,
            "bridgeLogTail": combined_log[-1200:] if combined_log else None,
            "hosted": hosted,
            "enabled": True,
        }


def get_whatsapp_diagnostics() -> dict[str, Any]:
    from paths import data_dir

    proxy_tail = read_proxy_log_tail()
    bridge_tail = read_bridge_log_tail()
    return {
        "enabled": whatsapp_enabled(),
        "hosted": is_cloud_deployment(),
        "bridgeUrl": BRIDGE_URL,
        "publicPort": BRIDGE_PUBLIC_PORT,
        "internalPort": BRIDGE_INTERNAL_PORT,
        "publicHealth": bridge_is_running(timeout=3),
        "internalHealth": internal_bridge_is_running(timeout=3),
        "node": _node_executable() or None,
        "bridgeDirExists": BRIDGE_DIR.is_dir(),
        "nodeModulesExists": (BRIDGE_DIR / "node_modules").is_dir(),
        "proxyLogTail": proxy_tail[-800:] if proxy_tail else None,
        "bridgeLogTail": bridge_tail[-800:] if bridge_tail else None,
        "dataDir": str(data_dir()),
    }


def _clear_local_whatsapp_session() -> None:
    import shutil

    from paths import whatsapp_auth_dir

    auth_dir = whatsapp_auth_dir()
    shutil.rmtree(auth_dir, ignore_errors=True)
    auth_dir.mkdir(parents=True, exist_ok=True)


def _request_bridge_restart() -> None:
    from paths import whatsapp_auth_dir

    auth_dir = whatsapp_auth_dir()
    auth_dir.mkdir(parents=True, exist_ok=True)
    lock = auth_dir / ".bridge.lock"
    flag = auth_dir / ".bridge-restart-requested"
    try:
        lock.unlink(missing_ok=True)
        flag.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def read_bridge_log_tail(*, max_lines: int = 40) -> str:
    from paths import data_dir

    log_path = data_dir() / "whatsapp-bridge.log"
    if not log_path.is_file():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


def read_proxy_log_tail(*, max_lines: int = 30) -> str:
    from paths import data_dir

    log_path = data_dir() / "whatsapp-proxy.log"
    if not log_path.is_file():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


def reset_bridge_session(*, force: bool = False) -> dict[str, Any]:
    try:
        return _bridge_request("/reset", method="POST", payload={"force": force})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
            return {"ok": False, **err}
        except json.JSONDecodeError:
            return {"ok": False, "error": body or str(exc)}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        _clear_local_whatsapp_session()
        _request_bridge_restart()
        return {
            "ok": True,
            "restarted": True,
            "message": "WhatsApp scanner is restarting — wait 60–90 seconds for a fresh QR code.",
        }


def send_bill_via_whatsapp(bill: dict[str, Any]) -> dict[str, Any]:
    phone = normalize_whatsapp_phone(bill.get("customerPhone", ""))
    if len(phone) < 12:
        raise ValueError("Valid 10-digit customer phone number is required.")

    message = build_whatsapp_message(bill)
    pdf_path = generate_invoice_pdf(bill)
    filename = invoice_filename(bill)

    status = get_bridge_status()
    if not status.get("ready") and status.get("sessionRestoring"):
        deadline = time.monotonic() + (120 if is_cloud_deployment() else 60)
        while time.monotonic() < deadline:
            time.sleep(2)
            status = get_bridge_status(auto_start=True)
            if status.get("ready"):
                break

    if not status.get("ready"):
        return {
            "sent": False,
            "reason": "not_connected",
            "bridgeAvailable": status.get("available", False),
            "pdfPath": str(pdf_path),
            "filename": filename,
            "message": message,
        }

    last_error = "Failed to send on WhatsApp."
    needs_reconnect = False
    for attempt in range(4):
        try:
            result = _bridge_request(
                "/send",
                method="POST",
                payload={
                    "phone": phone,
                    "message": message,
                    "pdfPath": str(pdf_path),
                    "filename": filename,
                },
                timeout=BRIDGE_SEND_TIMEOUT,
            )
            return {
                "sent": bool(result.get("ok")),
                "reason": "sent" if result.get("ok") else "send_failed",
                "filename": filename,
                "message": message,
            }
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retry_after = 5
            try:
                err = json.loads(body)
                last_error = err.get("error", body)
                needs_reconnect = bool(err.get("needsReconnect"))
                retry_after = int(err.get("retryAfterSec") or retry_after)
            except json.JSONDecodeError:
                last_error = body or str(exc)
                needs_reconnect = "detached frame" in last_error.lower()
            if exc.code in (429, 503) and attempt < 3:
                time.sleep(max(5, min(retry_after, 15)))
                continue
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc) if str(exc) else "WhatsApp send timed out — try again."
            if attempt < 3:
                time.sleep(3)
                continue
            break

    return {
        "sent": False,
        "reason": "send_failed",
        "error": last_error,
        "needsReconnect": needs_reconnect,
        "filename": filename,
        "message": message,
    }


def get_or_create_invoice_pdf(bill: dict[str, Any]) -> Path:
    """Always regenerate so WhatsApp/download PDFs match the latest template."""
    return generate_invoice_pdf(bill)


def send_offer_image_whatsapp(phone: str, message: str, image_path: Path) -> dict[str, Any]:
    digits = normalize_whatsapp_phone(phone)
    if len(digits) < 12:
        return {"sent": False, "reason": "invalid_phone", "error": "Invalid phone number."}

    if not image_path.is_file():
        return {"sent": False, "reason": "missing_image", "error": "Offer image not found."}

    status = get_bridge_status()
    if not status.get("ready"):
        return {
            "sent": False,
            "reason": "not_connected",
            "error": status.get("lastError") or "WhatsApp not connected.",
        }

    try:
        result = _bridge_request(
            "/send-image",
            method="POST",
            payload={
                "phone": digits,
                "message": message,
                "imagePath": str(image_path.resolve()),
                "filename": image_path.name,
            },
        )
        return {
            "sent": bool(result.get("ok")),
            "reason": "sent" if result.get("ok") else "send_failed",
            "error": result.get("error"),
        }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
            error = err.get("error", body)
        except json.JSONDecodeError:
            error = body or str(exc)
        return {"sent": False, "reason": "send_failed", "error": error}
