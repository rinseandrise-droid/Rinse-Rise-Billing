"""Flask API server for Rinse & Rise billing (PostgreSQL or SQLite backend)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, send_file

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from werkzeug.exceptions import HTTPException

from db import get_connection, is_postgres

from paths import ensure_data_dirs, invoice_dir, persistence_status

from database import (
    build_customer_profile,
    create_bill,
    create_expenditure,
    clear_all_data,
    delete_expenditure,
    ensure_database,
    get_all_bills,
    get_all_expenditures,
    get_bill_by_id,
    get_bill_counter,
    get_bills_by_phone,
    get_customer_favorite,
    get_overall_stats,
    get_profit_loss_summary,
    init_db,
    list_unique_customer_phones,
    migrate_from_local,
    normalize_phone_key,
    database_backend_name,
    database_config_status,
    set_customer_favorite,
    update_bill,
    update_bill_status,
    mark_bill_sent_via,
)
from invoice_pdf import build_whatsapp_message, generate_invoice_pdf, invoice_filename
from offer_broadcast import get_broadcast_job, save_offer_image, start_offer_broadcast
from offers import get_offers, save_offers
from rates import get_rates, save_rates
from whatsapp_send import (
    bridge_health_snapshot,
    bridge_is_running,
    ensure_hosted_whatsapp_stack,
    get_bridge_status,
    get_or_create_invoice_pdf,
    get_whatsapp_diagnostics,
    is_cloud_deployment,
    reset_bridge_session,
    send_bill_via_whatsapp,
    try_start_bridge,
    whatsapp_enabled,
)

app = Flask(__name__, static_folder=str(ROOT), static_url_path="")

_DB_API_PREFIXES = (
    "/api/bills",
    "/api/customers",
    "/api/settings",
    "/api/expenditures",
    "/api/reports",
    "/api/migrate",
    "/api/offers",
)


def _friendly_db_error(exc: Exception) -> str:
    msg = str(exc)
    if "postgres.railway.internal" in msg and "could not translate host name" in msg.lower():
        return (
            "PostgreSQL internal URL is not reachable. "
            "Open Railway → Rinse-RiseBilling → Variables → Raw Editor and use only the public URL: "
            "DATABASE_URL=${{Postgres.DATABASE_PUBLIC_URL}} "
            "(remove any DATABASE_URL that uses postgres.railway.internal), then redeploy."
        )
    if "deadlock detected" in msg.lower():
        return (
            "Database was busy for a moment (deadlock). "
            "Please click Send on WhatsApp again — your bill may already be saved in History."
        )
    if "duplicate key" in msg.lower() or "unique constraint" in msg.lower():
        return "Could not save this bill number because it already exists. Please try Save Bill again."
    if "Database not ready:" in msg:
        return msg.replace("Database not ready: ", "", 1)
    return msg


@app.before_request
def prepare_database():
    if request.method == "OPTIONS":
        return None
    if not any(request.path.startswith(prefix) for prefix in _DB_API_PREFIXES):
        return None
    try:
        ensure_database()
    except Exception as exc:
        return jsonify({"error": _friendly_db_error(exc), "code": "database_unavailable"}), 503


@app.errorhandler(Exception)
def handle_api_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    if request.path.startswith("/api/"):
        app.logger.exception("API error on %s", request.path)
        return jsonify({"error": str(exc)}), 500
    raise exc


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return response


@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/api/live")
def live():
    """Fast liveness probe for Railway — no DB or WhatsApp checks."""
    return jsonify({"ok": True})


@app.route("/api/health")
def health():
    config = database_config_status()
    wa_on = whatsapp_enabled()
    wa_snapshot = bridge_health_snapshot(timeout=2) if wa_on else {"available": False, "ready": False}
    db_ok = False
    db_error = None
    try:
        ensure_database()
        with get_connection() as conn:
            conn.execute("SELECT 1")
        db_ok = True
    except Exception as exc:
        db_error = str(exc)
    fix_steps: list[str] = []
    if not db_ok and is_postgres():
        fix_steps = [
            "Open Rinse-RiseBilling → Variables → remove any old manual DATABASE_URL",
            "Add Variable Reference: PostgreSQL service → DATABASE_PRIVATE_URL → name it DATABASE_URL",
            "If that fails, also add DATABASE_PUBLIC_URL from the Postgres service",
            "Redeploy Rinse-RiseBilling and check /api/health for dbOk: true",
        ]
        if db_error and "railway.internal" in db_error:
            fix_steps.insert(
                0,
                "DATABASE_URL points to postgres.railway.internal but this service cannot reach Postgres — re-link using Variable Reference (do not paste an old copied URL).",
            )
    return jsonify(
        {
            "ok": True,
            **config,
            "dbOk": db_ok,
            "dbError": db_error,
            "fixSteps": fix_steps or config.get("fixSteps"),
            "whatsappEnabled": wa_on,
            "whatsappAvailable": wa_snapshot.get("available", False),
            "whatsappReady": wa_snapshot.get("ready", False),
            "whatsappPhase": wa_snapshot.get("phase"),
            "hosted": is_cloud_deployment(),
            "persistence": persistence_status(),
        }
    )


@app.route("/api/settings/bill-counter")
def api_bill_counter():
    return jsonify({"billCounter": get_bill_counter()})


@app.route("/api/bills", methods=["GET"])
def api_list_bills():
    return jsonify(get_all_bills())


@app.route("/api/bills", methods=["POST"])
def api_create_bill():
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("items"):
        return jsonify({"error": "Bill must have at least one item"}), 400
    try:
        bill = create_bill(data)
    except Exception as exc:
        return jsonify({"error": _friendly_db_error(exc)}), 500
    return jsonify(bill), 201


@app.route("/api/bills/<int:bill_id>", methods=["GET"])
def api_get_bill(bill_id: int):
    bill = get_bill_by_id(bill_id)
    if not bill:
        return jsonify({"error": "Bill not found"}), 404
    return jsonify(bill)


@app.route("/api/bills/<int:bill_id>", methods=["PUT"])
def api_update_bill(bill_id: int):
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("items"):
        return jsonify({"error": "Bill must have at least one item"}), 400
    try:
        bill = update_bill(bill_id, data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not bill:
        return jsonify({"error": "Bill not found"}), 404
    return jsonify(bill)


@app.route("/api/bills/<int:bill_id>/status", methods=["PATCH"])
def api_update_status(bill_id: int):
    data = request.get_json(force=True, silent=True) or {}
    status = data.get("deliveryStatus", "pending")
    if status not in ("pending", "ready", "done"):
        return jsonify({"error": "Invalid status"}), 400
    bill = update_bill_status(bill_id, status)
    if not bill:
        return jsonify({"error": "Bill not found"}), 404
    return jsonify(bill)


@app.route("/api/customers/<phone_key>/profile")
def api_customer_profile(phone_key: str):
    key = normalize_phone_key(phone_key)
    profile = build_customer_profile(key)
    orders = get_bills_by_phone(key)
    return jsonify({"profile": profile, "orders": orders})


@app.route("/api/customers/<phone_key>/favorite", methods=["GET"])
def api_get_customer_favorite(phone_key: str):
    key = normalize_phone_key(phone_key)
    return jsonify({"phoneKey": key, "isFavorite": get_customer_favorite(key)})


@app.route("/api/customers/<phone_key>/favorite", methods=["PATCH"])
def api_set_customer_favorite(phone_key: str):
    key = normalize_phone_key(phone_key)
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = set_customer_favorite(
            key,
            data.get("phone", key),
            data.get("name", ""),
            bool(data.get("isFavorite")),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/expenditures", methods=["GET"])
def api_list_expenditures():
    date_from = request.args.get("from", "").strip() or None
    date_to = request.args.get("to", "").strip() or None
    items = get_all_expenditures(date_from, date_to)
    total = sum(e["amount"] for e in items)
    return jsonify({"items": items, "total": total, "count": len(items), "dateFrom": date_from or "", "dateTo": date_to or ""})


@app.route("/api/expenditures", methods=["POST"])
def api_create_expenditure():
    data = request.get_json(force=True, silent=True) or {}
    try:
        item = create_expenditure(
            data.get("name", ""),
            float(data.get("amount", 0)),
            data.get("date"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except TypeError:
        return jsonify({"error": "Invalid amount"}), 400
    return jsonify(item), 201


@app.route("/api/expenditures/<int:expenditure_id>", methods=["DELETE"])
def api_delete_expenditure(expenditure_id: int):
    if not delete_expenditure(expenditure_id):
        return jsonify({"error": "Expenditure not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/reports/overall")
def api_overall_stats():
    return jsonify(get_overall_stats())


@app.route("/api/reports/profit-loss")
def api_profit_loss():
    date_from = request.args.get("from", "").strip() or None
    date_to = request.args.get("to", "").strip() or None
    return jsonify(get_profit_loss_summary(date_from, date_to))


@app.route("/api/bills/<int:bill_id>/invoice.pdf")
def api_bill_invoice_pdf(bill_id: int):
    bill = get_bill_by_id(bill_id)
    if not bill:
        return jsonify({"error": "Bill not found"}), 404
    pdf_path = get_or_create_invoice_pdf(bill)
    response = send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=invoice_filename(bill),
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@app.route("/api/bills/<int:bill_id>/send-whatsapp", methods=["POST"])
def api_send_bill_whatsapp(bill_id: int):
    bill = get_bill_by_id(bill_id)
    if not bill:
        return jsonify({"error": "Bill not found"}), 404
    if not bill.get("customerPhone"):
        return jsonify({"error": "Customer phone number is required."}), 400
    payload = request.get_json(silent=True) or {}
    skip_payment = bool(payload.get("skipPaymentValidation"))
    if not skip_payment:
        if not (bill.get("paymentType") or "").strip():
            return jsonify({"error": "Payment Type is required before sending on WhatsApp."}), 400
        if not (bill.get("paymentInfo") or "").strip():
            return jsonify({"error": "Payment Info is required before sending on WhatsApp."}), 400
    if (bill.get("deliveryStatus") or "").strip() != "done" and (bill.get("sentVia") or "").strip() != "whatsapp":
        return jsonify({"error": "Order must be marked Delivery Done before sending on WhatsApp."}), 400
    try:
        result = send_bill_via_whatsapp(bill)
        if result.get("sent"):
            mark_bill_sent_via(bill_id, "whatsapp")
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/whatsapp/status")
def api_whatsapp_status():
    auto_start = request.args.get("start", "").lower() in ("1", "true", "yes")
    return jsonify(get_bridge_status(auto_start=auto_start))


@app.route("/api/whatsapp/start", methods=["POST"])
def api_whatsapp_start():
    if not whatsapp_enabled():
        return jsonify({"ok": False, "error": "WhatsApp is disabled on this server.", "enabled": False})
    wait = 45 if is_cloud_deployment() else 10
    started = (
        ensure_hosted_whatsapp_stack(wait_seconds=wait)
        if is_cloud_deployment()
        else try_start_bridge(wait_seconds=wait)
    )
    status = get_bridge_status()
    return jsonify({"ok": started or status.get("available"), **status})


@app.route("/api/whatsapp/diagnostics")
def api_whatsapp_diagnostics():
    return jsonify(get_whatsapp_diagnostics())


@app.route("/api/whatsapp/reset", methods=["POST"])
def api_whatsapp_reset():
    return jsonify(reset_bridge_session())


@app.route("/api/migrate", methods=["POST"])
def api_migrate():
    data = request.get_json(force=True, silent=True) or {}
    result = migrate_from_local(data)
    return jsonify(result)


@app.route("/api/rates", methods=["GET"])
def api_get_rates():
    try:
        return jsonify(get_rates())
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/rates", methods=["PUT"])
def api_save_rates():
    data = request.get_json(force=True, silent=True) or {}
    password = (data.get("password") or request.headers.get("X-Rates-Password") or "").strip()
    expected = os.environ.get("OFFERS_EDIT_PASSWORD", os.environ.get("CLEAR_DATA_PASSWORD", "NihkilDada@22")).strip()
    if not password or password != expected:
        return jsonify({"error": "Invalid password"}), 403
    payload = {k: v for k, v in data.items() if k != "password"}
    try:
        rates = save_rates(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(rates)


@app.route("/api/customers/unique-phones")
def api_unique_customer_phones():
    try:
        customers = list_unique_customer_phones()
    except Exception as exc:
        return jsonify({"error": _friendly_db_error(exc)}), 503
    return jsonify({"total": len(customers), "customers": customers})


@app.route("/api/offers/broadcast", methods=["POST"])
def api_start_offer_broadcast():
    message = (request.form.get("message") or "").strip()
    upload = request.files.get("image")
    if not upload or not upload.filename:
        return jsonify({"error": "Offer photo is required."}), 400
    try:
        image_path = save_offer_image(upload)
        result = start_offer_broadcast(message, image_path)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Offer broadcast failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/offers/broadcast/<job_id>")
def api_offer_broadcast_status(job_id: str):
    job = get_broadcast_job(job_id)
    if not job:
        return jsonify({"error": "Broadcast job not found."}), 404
    return jsonify(job)


@app.route("/api/offers", methods=["GET"])
def api_get_offers():
    return jsonify({"offers": get_offers()})


@app.route("/api/offers", methods=["PUT"])
def api_save_offers():
    data = request.get_json(force=True, silent=True) or {}
    password = (data.get("password") or request.headers.get("X-Offers-Password") or "").strip()
    expected = os.environ.get("OFFERS_EDIT_PASSWORD", os.environ.get("CLEAR_DATA_PASSWORD", "NihkilDada@22")).strip()
    if not password or password != expected:
        return jsonify({"error": "Invalid password"}), 403
    raw_offers = data.get("offers")
    if not isinstance(raw_offers, list):
        return jsonify({"error": "Offers must be a list."}), 400
    try:
        offers = save_offers(raw_offers)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"offers": offers})


@app.route("/api/admin/clear-data", methods=["POST"])
def api_clear_all_data():
    data = request.get_json(force=True, silent=True) or {}
    password = (data.get("password") or request.headers.get("X-Clear-Data-Password") or "").strip()
    expected = os.environ.get("CLEAR_DATA_PASSWORD", "NihkilDada@22").strip()
    if not password or password != expected:
        return jsonify({"error": "Invalid password"}), 403
    result = clear_all_data()
    return jsonify({"ok": True, **result})


@app.route("/<path:path>", methods=["GET", "HEAD"])
def static_files(path):
    if path.startswith("api/"):
        return jsonify({"error": "Not found"}), 404
    response = send_from_directory(ROOT, path)
    if path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def bootstrap_app() -> None:
    """Run once on import so gunicorn/Docker loads the DB schema."""
    config = database_config_status()
    if config.get("warning"):
        print(f"WARNING: {config['warning']}")
        for step in config.get("fixSteps", []):
            print(f"  → {step}")
    ensure_data_dirs()
    try:
        ensure_database()
        print(f"Rinse & Rise Billing — {config['backend']} database ready")
        if config.get("postgresEnvVar"):
            print(f"PostgreSQL connected via {config['postgresEnvVar']}")
        persist = persistence_status()
        print(f"Data directory: {persist['dataDir']}")
        if persist.get("volumeRecommended"):
            print("TIP: Mount a Railway Volume at /app/data to keep bills & WhatsApp session after redeploy")
    except Exception as exc:
        print(f"ERROR: Database init failed ({exc}). Will retry on first API request.")

    if whatsapp_enabled() and not is_cloud_deployment():
        if try_start_bridge():
            print("WhatsApp bridge starting — logs: whatsapp-bridge/bridge.log")
        elif bridge_is_running():
            print("WhatsApp bridge already running on port 3001")
        else:
            print("WhatsApp bridge not running — install Node.js and run Start Billing.bat")
    elif whatsapp_enabled() and is_cloud_deployment():
        print("WhatsApp bridge managed by container entrypoint (single instance)")


bootstrap_app()


if __name__ == "__main__":
    init_db()
    backend = database_backend_name()
    print(f"Rinse & Rise Billing — {backend} database ready (dev server)")
    print("API routes: bills, invoice PDF, WhatsApp send")
    if not is_cloud_deployment():
        if try_start_bridge():
            print("WhatsApp bridge started — logs: whatsapp-bridge/bridge.log")
        elif bridge_is_running():
            print("WhatsApp bridge already running on port 3001")
        else:
            print("WhatsApp bridge not running — install Node.js and run Start Billing.bat")

    port = int(os.environ.get("PORT", 8080))
    print(f"Open http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)


def _kickoff_hosted_whatsapp() -> None:
    if is_cloud_deployment() and whatsapp_enabled():
        ensure_hosted_whatsapp_stack(wait_seconds=25)


threading.Thread(target=_kickoff_hosted_whatsapp, daemon=True).start()
