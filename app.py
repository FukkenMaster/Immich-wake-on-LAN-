import os
import sys
import json
import socket
import time
import threading
import logging
from logging.handlers import TimedRotatingFileHandler
import urllib.request
from flask import Flask, jsonify, send_from_directory, request

TARGET_IP = os.environ.get("TARGET_IP", "192.168.0.116")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "2283"))
TARGET_MAC = os.environ.get("TARGET_MAC", "3C:52:82:72:31:9B")
WAKE_TIMEOUT = int(os.environ.get("WAKE_TIMEOUT", "180"))
WAKE_COOLDOWN = int(os.environ.get("WAKE_COOLDOWN", "20"))
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "2"))
PORT = int(os.environ.get("PORT", "18080"))
SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/data/settings.json")

app = Flask(__name__)
lock = threading.Lock()
state = {"wake_started_at": None, "last_wake_sent_at": 0.0}
settings = {"auto_wake": True}
log_state = {"last_up": None, "timeout_logged": False, "auto_suppressed_until": 0.0}

# --- event logging (important events only, rotated) ---
event_logger = logging.getLogger("wake_events")
event_logger.setLevel(logging.INFO)
event_logger.propagate = False
if not event_logger.handlers:
    _fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    _fmt.converter = time.gmtime
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    event_logger.addHandler(_sh)
    try:
        _log_dir = os.path.dirname(SETTINGS_PATH) or "/data"
        os.makedirs(_log_dir, exist_ok=True)
        _fh = TimedRotatingFileHandler(
            os.path.join(_log_dir, "wake-events.log"),
            when="D",
            interval=1,
            backupCount=5,
            utc=True,
        )
        _fh.setFormatter(_fmt)
        event_logger.addHandler(_fh)
    except Exception:
        pass


def log_event(event, **fields):
    parts = [f"event={event}"]
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v).replace("\n", " ").replace("\r", " ")
        if len(s) > 300:
            s = s[:300] + "..."
        parts.append(f"{k}={s}")
    try:
        event_logger.info(" ".join(parts))
    except Exception:
        pass


def get_client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or "-"


def get_ua():
    ua = request.headers.get("User-Agent") or "-"
    return ua[:200]


def req_ctx(trigger):
    return {
        "trigger": trigger,
        "client_ip": get_client_ip(),
        "ua": get_ua(),
        "orig_uri": request.headers.get("X-Original-URI"),
    }


def load_settings():
    global settings
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "auto_wake" in data:
            settings["auto_wake"] = bool(data["auto_wake"])
    except Exception:
        pass


def save_settings():
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f)
        os.replace(tmp, SETTINGS_PATH)
    except Exception:
        pass


load_settings()


def probe():
    url = f"http://{TARGET_IP}:{TARGET_PORT}/api/server/ping"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "immich-wake-helper/1"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def send_wol(mac):
    mac_bytes = bytes.fromhex(mac.replace("-", "").replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ("255.255.255.255", 9))


def get_status(do_wake=False, manual=False, ctx=None):
    """do_wake: attempt to trigger WoL if appropriate.
    - manual=True: explicit user action (e.g. "Wake server"/"Try again" button), always allowed.
    - manual=False with do_wake=True: automatic trigger, only allowed when global auto_wake is enabled.
    """
    ctx = ctx or {}
    now = time.time()
    up = probe()
    with lock:
        auto_wake_enabled = settings.get("auto_wake", True)

        if up:
            if log_state.get("last_up") is False:
                duration = None
                if state.get("wake_started_at"):
                    duration = int(now - state["wake_started_at"])
                log_event("server_up", wake_duration_s=duration)
            log_state["last_up"] = True
            log_state["timeout_logged"] = False
            log_state["auto_suppressed_until"] = 0.0
            state["wake_started_at"] = None
            return {"state": "up", "auto_wake": auto_wake_enabled}

        # server is down/unreachable
        if log_state.get("last_up") is not False:
            log_state["last_up"] = False
            log_state["timeout_logged"] = False

        if do_wake:
            if manual or auto_wake_enabled:
                if state["wake_started_at"] is None or (now - state["wake_started_at"] > WAKE_TIMEOUT):
                    state["wake_started_at"] = now
                    log_state["timeout_logged"] = False
                if now - state["last_wake_sent_at"] >= WAKE_COOLDOWN:
                    try:
                        send_wol(TARGET_MAC)
                        log_event(
                            "wol_sent",
                            manual=manual,
                            trigger=ctx.get("trigger"),
                            client_ip=ctx.get("client_ip"),
                            ua=ctx.get("ua"),
                            orig_uri=ctx.get("orig_uri"),
                            target=f"{TARGET_IP}({TARGET_MAC})",
                            elapsed_s=int(now - state["wake_started_at"]),
                        )
                    except Exception as e:
                        log_event(
                            "wol_send_error",
                            manual=manual,
                            trigger=ctx.get("trigger"),
                            client_ip=ctx.get("client_ip"),
                            ua=ctx.get("ua"),
                            error=str(e),
                        )
                    state["last_wake_sent_at"] = now
            else:
                # auto trigger requested but global auto-wake is disabled: log sparingly
                if now >= log_state.get("auto_suppressed_until", 0.0):
                    log_event(
                        "auto_wake_suppressed",
                        trigger=ctx.get("trigger"),
                        client_ip=ctx.get("client_ip"),
                        ua=ctx.get("ua"),
                        orig_uri=ctx.get("orig_uri"),
                    )
                    log_state["auto_suppressed_until"] = now + 3600

        if state["wake_started_at"] is None:
            return {"state": "down", "auto_wake": auto_wake_enabled}

        elapsed = now - state["wake_started_at"]
        if elapsed <= WAKE_TIMEOUT:
            return {"state": "waking", "seconds": int(elapsed), "auto_wake": auto_wake_enabled}

        if not log_state.get("timeout_logged"):
            log_event("wake_timeout", elapsed_s=int(elapsed))
            log_state["timeout_logged"] = True
        return {"state": "timeout", "seconds": int(elapsed), "auto_wake": auto_wake_enabled}


@app.route("/__wake/settings", methods=["GET"])
def get_settings():
    with lock:
        return jsonify({"auto_wake": bool(settings.get("auto_wake", True))})


@app.route("/__wake/settings", methods=["POST"])
def update_settings():
    data = request.get_json(silent=True) or {}
    with lock:
        if "auto_wake" in data:
            settings["auto_wake"] = bool(data["auto_wake"])
            save_settings()
        return jsonify({"auto_wake": bool(settings.get("auto_wake", True))})


@app.route("/__wake/status")
def status():
    return jsonify(get_status(do_wake=False))


@app.route("/__wake/wake", methods=["POST", "GET"])
def wake():
    manual = request.args.get("manual") in ("1", "true", "yes")
    trigger = "manual_button" if manual else "browser_auto"
    return jsonify(get_status(do_wake=True, manual=manual, ctx=req_ctx(trigger)))


@app.route("/__wake/", defaults={"subpath": ""})
@app.route("/__wake/<path:subpath>")
def wake_entry(subpath):
    # Serve the interactive UI only for the wake page itself.
    if subpath not in ("", "index.html"):
        # Unknown subpath under /__wake/: treat as intercepted/API-style request.
        s = get_status(do_wake=True, manual=False, ctx=req_ctx("intercepted_request"))
        return jsonify({"error": "server_unavailable", "message": "Immich is currently offline/unavailable.", "wake": s}), 503

    accept = request.headers.get("Accept", "")
    sec_mode = request.headers.get("Sec-Fetch-Mode", "")
    sec_dest = request.headers.get("Sec-Fetch-Dest", "")

    is_navigation = sec_mode == "navigate" or sec_dest == "document"
    prefers_html = "text/html" in accept

    if is_navigation or prefers_html:
        # Browser loading the wake page: serve UI; JS handles auto/manual wake using global setting.
        return send_from_directory(".", "index.html"), 200

    # Non-browser/API client (e.g. Immich mobile app) hit while server is down:
    # auto-trigger wake only if global auto_wake is enabled.
    s = get_status(do_wake=True, manual=False, ctx=req_ctx("intercepted_request"))
    return jsonify({"error": "server_unavailable", "message": "Immich is currently offline/unavailable.", "wake": s}), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
