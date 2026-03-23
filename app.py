from flask import Flask, render_template, request, jsonify
import json
import os

app = Flask(__name__)

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


if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE):
        save_json(CONFIG_FILE, DEFAULT_CONFIG)
    if not os.path.exists(JOBS_FILE):
        save_json(JOBS_FILE, {"last_scan": None, "jobs": []})
    print("Job Agent running at http://localhost:5001")
    app.run(debug=True, port=5001)
