from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
from functools import wraps
from typing import Any, Dict, List, Optional

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
    url_for,
)
from werkzeug.utils import secure_filename

from .airprint import ensure_airprint_via_root_helper
from .config import ConfigStore
from .imageproc import SUPPORTED_IMAGE_EXTS, prepare_print_file, render_preview_png
from .printer import (
    cups_available,
    get_default_printer,
    get_default_printer_display,
    job_stats,
    list_printers,
    printer_detail,
    print_file,
    queue_jobs,
    scheduler_status,
)
from .util import PrinterPalError, human_bytes, run_cmd


UPLOAD_DIR = os.environ.get("PRINTERPAL_UPLOAD_DIR", "/var/lib/printerpal/uploads")
CACHE_DIR = os.environ.get("PRINTERPAL_CACHE_DIR", "/var/lib/printerpal/cache")
ROOT_HELPER = os.environ.get("PRINTERPAL_ROOT_HELPER", "/usr/local/sbin/printerpal-root")

ALLOWED_EXTS = {".pdf", *SUPPORTED_IMAGE_EXTS}


def _ensure_dirs() -> None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)


def _list_uploads(limit: int = 25) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    try:
        names = os.listdir(UPLOAD_DIR)
    except FileNotFoundError:
        return files

    for name in names:
        path = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        files.append(
            {
                "name": name,
                "size": int(st.st_size),
                "size_h": human_bytes(int(st.st_size)),
                "mtime": int(st.st_mtime),
            }
        )

    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files[: max(1, min(limit, 200))]


def _allowed_filename(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTS


def _require_token(app: Flask):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            cfg = app.config["PP_CFG"]
            sec = cfg.get("security", {})
            if not sec.get("require_token"):
                return fn(*args, **kwargs)
            expected = (sec.get("token") or "").strip()
            if not expected:
                abort(503, description="Auth token required but not configured")
            provided = request.headers.get("X-PrinterPal-Token") or request.args.get("token")
            if provided != expected:
                abort(401)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def create_app() -> Flask:
    _ensure_dirs()

    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
    )

    # Logging: gunicorn/systemd will capture stdout/err; keep this simple and structured.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("printerpal")

    store = ConfigStore()
    cfg = store.load()

    app.config["PP_STORE"] = store
    app.config["PP_CFG"] = cfg
    app.config["PP_AIRPRINT_LAST_ENSURE"] = 0.0
    app.config["PP_AIRPRINT_LAST_SIG"] = ""
    app.config["PP_AIRPRINT_LOCK"] = threading.Lock()
    app.secret_key = cfg["app"]["secret_key"]

    # Flask upload limit.
    app.config["MAX_CONTENT_LENGTH"] = int(cfg["app"]["max_upload_mb"]) * 1024 * 1024

    # Best-effort AirPrint on startup.
    if cfg.get("airprint", {}).get("auto_enable"):
        try:
            ensure_airprint_via_root_helper()
        except Exception as e:
            # Non-fatal: show in UI under status.
            log.warning("AirPrint ensure failed at startup: %s", e)

    require_token = _require_token(app)

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "cups": cups_available()})

    @app.route("/")
    def index():
        cfg = app.config["PP_CFG"]
        return render_template(
            "index.html",
            ui_defaults=cfg.get("ui", {}),
            default_mode=cfg.get("printing", {}).get("default_mode", "grayscale"),
        )

    @app.route("/uploads/<path:filename>")
    def downloads(filename: str):
        # Only serve from upload directory.
        return send_from_directory(UPLOAD_DIR, filename, as_attachment=True)

    @app.route("/upload", methods=["POST"])
    def upload():
        if "file" not in request.files:
            abort(400, description="No file part")
        file = request.files["file"]
        if not file or file.filename is None:
            abort(400, description="No file provided")

        filename = secure_filename(file.filename)
        if not filename:
            abort(400, description="Invalid filename")
        if not _allowed_filename(filename):
            abort(415, description="Unsupported file type. Use PDF or common image formats.")

        # Avoid clobber: add timestamp suffix if exists.
        base, ext = os.path.splitext(filename)
        outname = filename
        outpath = os.path.join(UPLOAD_DIR, outname)
        if os.path.exists(outpath):
            outname = f"{base}_{int(time.time())}{ext}"
            outpath = os.path.join(UPLOAD_DIR, outname)

        file.save(outpath)
        return redirect(url_for("index"))

    @app.route("/api/files")
    def api_files():
        return jsonify({"files": _list_uploads(limit=50)})

    @app.route("/api/status")
    def api_status():
        cfg = app.config["PP_CFG"]
        printers = [p.__dict__ for p in list_printers()] if cups_available() else []
        default = get_default_printer()
        default_display = get_default_printer_display()
        default_label = f"{default_display} (default)" if default_display else ""
        stats = job_stats() if cups_available() else {}
        jobs = queue_jobs() if cups_available() else []

        auto_airprint = bool(cfg.get("airprint", {}).get("auto_enable"))
        airprint_state: Dict[str, Any] = {"enabled": auto_airprint}

        # Best-effort, rate-limited auto AirPrint ensure.
        if auto_airprint and cups_available():
            try:
                sig = ",".join(sorted([p.get("name", "") for p in printers if p.get("name")]))
                now = time.monotonic()
                last_t = float(app.config.get("PP_AIRPRINT_LAST_ENSURE") or 0.0)
                last_sig = str(app.config.get("PP_AIRPRINT_LAST_SIG") or "")
                # Re-run if printer set changed or every 10 minutes.
                if sig != last_sig or (now - last_t) > 600.0:
                    lock = app.config["PP_AIRPRINT_LOCK"]
                    if lock.acquire(blocking=False):
                        try:
                            ensure_airprint_via_root_helper(timeout_s=45.0)
                            app.config["PP_AIRPRINT_LAST_ENSURE"] = now
                            app.config["PP_AIRPRINT_LAST_SIG"] = sig
                        finally:
                            lock.release()
            except Exception:
                # Non-fatal: AirPrint may not be available in all deployments.
                pass

        return jsonify(
            {
                "cups_available": cups_available(),
                "scheduler": scheduler_status(),
                "default_printer": default,
                "default_printer_display": default_display,
                "default_printer_label": default_label,
                "printers": printers,
                "jobs": jobs,
                "stats": stats,
                "airprint": airprint_state,
            }
        )

    @app.route("/api/printer/<name>")
    def api_printer_detail(name: str):
        return jsonify(printer_detail(name))

    @app.route("/api/config", methods=["GET"])
    def api_config_get():
        return jsonify({"config": app.config["PP_CFG"]})

    @app.route("/api/config", methods=["POST"])
    @require_token
    def api_config_set():
        store: ConfigStore = app.config["PP_STORE"]
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or "config" not in payload:
            abort(400, description="Expected JSON body: {config: {...}}")
        cfg_new = payload["config"]
        if not isinstance(cfg_new, dict):
            abort(400, description="config must be an object")

        store.save(cfg_new)
        cfg = store.load()
        app.config["PP_CFG"] = cfg
        app.config["MAX_CONTENT_LENGTH"] = int(cfg["app"]["max_upload_mb"]) * 1024 * 1024

        if cfg.get("airprint", {}).get("auto_enable"):
            try:
                ensure_airprint_via_root_helper()
            except Exception as e:
                return jsonify({"ok": False, "error": str(e), "config": cfg}), 500

        return jsonify({"ok": True, "config": cfg})

    @app.route("/api/preview/<path:filename>")
    def api_preview(filename: str):
        cfg = app.config["PP_CFG"]
        mode = request.args.get("mode", cfg.get("printing", {}).get("default_mode", "grayscale"))
        page = int(request.args.get("page", "1"))
        width = int(request.args.get("w", "720"))

        path = os.path.join(UPLOAD_DIR, filename)
        if not os.path.exists(path):
            abort(404)

        try:
            png = render_preview_png(
                path,
                mode=mode,
                page=page,
                width=width,
                preview_dpi=int(cfg["printing"]["preview_dpi"]),
                threshold=int(cfg["printing"]["bw_threshold"]),
            )
        except PrinterPalError as e:
            return Response(str(e), status=400, mimetype="text/plain")

        return Response(png, mimetype="image/png")

    @app.route("/api/print", methods=["POST"])
    @require_token
    def api_print():
        cfg = app.config["PP_CFG"]
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            abort(400, description="Invalid JSON")

        filename = str(payload.get("filename") or "").strip()
        if not filename:
            abort(400, description="filename required")

        mode = str(payload.get("mode") or cfg["printing"].get("default_mode", "grayscale")).strip().lower()
        printer = str(payload.get("printer") or "").strip() or None
        copies = int(payload.get("copies") or cfg["printing"].get("default_copies", 1))

        path = os.path.join(UPLOAD_DIR, filename)
        if not os.path.exists(path):
            abort(404, description="File not found")

        tmp_out: str | None = None
        try:
            prepared_path, meta = prepare_print_file(
                path,
                mode=mode,
                print_dpi=int(cfg["printing"]["print_dpi"]),
                max_pdf_pages=int(cfg["printing"]["max_pdf_pages_process"]),
                threshold=int(cfg["printing"]["bw_threshold"]),
            )
            if meta.get("prepared"):
                tmp_out = prepared_path

            res = print_file(
                prepared_path,
                printer=printer,
                copies=copies,
                title=f"PrinterPal: {filename}",
                options=[],
                timeout_s=60.0,
            )
        except PrinterPalError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        finally:
            if tmp_out and os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except OSError:
                    pass

        return jsonify({"ok": True, "lp_stdout": res.stdout.strip()})

    @app.route("/api/airprint/ensure", methods=["POST"])
    @require_token
    def api_airprint_ensure():
        try:
            out = ensure_airprint_via_root_helper(timeout_s=45.0)
            return jsonify(out)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/restart-host", methods=["POST"])
    @require_token
    def api_restart_host():
        # The actual reboot is delegated to the root helper.
        try:
            res = run_cmd(["sudo", "-n", ROOT_HELPER, "restart-host"], timeout_s=5.0, check=True)
            return jsonify({"ok": True, "output": (res.stdout or "").strip()})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/events")
    def events():
        def gen():
            while True:
                try:
                    status = {
                        "ts": int(time.time()),
                        "files": _list_uploads(limit=25),
                        "status": json.loads(api_status().get_data(as_text=True)),
                    }
                    yield f"event: status\ndata: {json.dumps(status)}\n\n"
                except Exception as e:
                    # Keep connection alive with an error event.
                    payload = {"ts": int(time.time()), "error": str(e)}
                    yield f"event: error\ndata: {json.dumps(payload)}\n\n"

                time.sleep(2.0)

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        return Response(stream_with_context(gen()), headers=headers)

    @app.errorhandler(PrinterPalError)
    def handle_pp_error(e: PrinterPalError):
        return jsonify({"ok": False, "error": str(e)}), 500

    return app
