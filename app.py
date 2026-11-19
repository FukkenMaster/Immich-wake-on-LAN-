import os
import json
import socket
import time
import threading
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


def get_status(do_wake=False, manual=False):
    """do_wake: attempt to trigger WoL if appropriate.
    - manual=True: explicit user action (e.g. "Wake server"/"Try again" button), always allowed.
    - manual=False with do_wake=True: automatic trigger, only allowed when global auto_wake is enabled.
    """
    now = time.time()
    up = probe()
    with lock:
        auto_wake_enabled = settings.get("auto_wake", True)
        if up:
            state["wake_started_at"] = None
            return {"state": "up", "auto_wake": auto_wake_enabled}

        if do_wake and (manual or auto_wake_enabled):
            if state["wake_started_at"] is None or (now - state["wake_started_at"] > WAKE_TIMEOUT):
                state["wake_started_at"] = now
            if now - state["last_wake_sent_at"] >= WAKE_COOLDOWN:
                try:
                    send_wol(TARGET_MAC)
                except Exception as e:
                    state["last_wake_sent_at"] = now
                    elapsed = int(now - state["wake_started_at"])
                    return {"state": "waking", "seconds": elapsed, "auto_wake": auto_wake_enabled, "detail": f"wake send error: {e}"}
                state["last_wake_sent_at"] = now

        if state["wake_started_at"] is None:
            return {"state": "down", "auto_wake": auto_wake_enabled}

        elapsed = now - state["wake_started_at"]
        if elapsed <= WAKE_TIMEOUT:
            return {"state": "waking", "seconds": int(elapsed), "auto_wake": auto_wake_enabled}
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
    return jsonify(get_status(do_wake=True, manual=manual))


@app.route("/__wake/", defaults={"subpath": ""})
@app.route("/__wake/<path:subpath>")
def wake_entry(subpath):
    # Serve the interactive UI only for the wake page itself.
    if subpath not in ("", "index.html"):
        # Unknown subpath under /__wake/: treat as intercepted/API-style request.
        s = get_status(do_wake=True, manual=False)
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
    s = get_status(do_wake=True, manual=False)
    return jsonify({"error": "server_unavailable", "message": "Immich is currently offline/unavailable.", "wake": s}), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
