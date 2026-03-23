from flask import Flask, render_template, request, jsonify, Response
import json
import os
import threading
import queue

app = Flask(__name__)

# ── Scan state ────────────────────────────────────────────────────────────────
_scan_lock   = threading.Lock()
_scan_state  = {"running": False, "log": [], "new": 0, "total": 0, "error": None}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
JOBS_FILE = os.path.join(BASE_DIR, "jobs.json")

DEFAULT_CONFIG = {
    "companies": ["NVIDIA", "Google", "Microsoft", "OpenAI", "Anthropic", "Poolside", "Mistral", "Cohere", "Nebius AI"],
    "roles": ["AI GTM", "Sales", "Account Executive", "Solutions Engineer", "Business Development", "AI Sales", "Field Sales", "Enterprise Sales"],
    "locations": ["Spain", "Remote Europe"],
    "email": "eordaxd@gmail.com",
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_json(CONFIG_FILE, DEFAULT_CONFIG))


@app.route("/api/config", methods=["POST"])
def update_config():
    config = request.get_json()
    if not config:
        return jsonify({"error": "Invalid JSON"}), 400
    save_json(CONFIG_FILE, config)
    return jsonify({"status": "saved"})


@app.route("/api/jobs", methods=["GET"])
def get_jobs():
    return jsonify(load_json(JOBS_FILE, {"last_scan": None, "jobs": []}))


@app.route("/api/jobs", methods=["POST"])
def update_jobs():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    save_json(JOBS_FILE, data)
    return jsonify({"status": "saved"})


@app.route("/api/scan", methods=["POST"])
def start_scan():
    with _scan_lock:
        if _scan_state["running"]:
            return jsonify({"error": "Scan already in progress"}), 409
        _scan_state.update({"running": True, "log": [], "new": 0, "total": 0, "error": None})

    def _run():
        from scanner import run_scan

        def _log(msg):
            with _scan_lock:
                _scan_state["log"].append(msg)

        try:
            new_count, total = run_scan(_log)
            with _scan_lock:
                _scan_state.update({"running": False, "new": new_count, "total": total})
        except Exception as exc:
            with _scan_lock:
                _scan_state.update({"running": False, "error": str(exc)})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/scan/status", methods=["GET"])
def scan_status():
    with _scan_lock:
        return jsonify(dict(_scan_state))


if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE):
        save_json(CONFIG_FILE, DEFAULT_CONFIG)
    if not os.path.exists(JOBS_FILE):
        save_json(JOBS_FILE, {"last_scan": None, "jobs": []})
    print("Job Agent running at http://localhost:5001")
    app.run(debug=True, port=5001)
