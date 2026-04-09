#!/usr/bin/env python3
# app.py
# Flask web server for the Website Problem Finder.
#
# Run with:
#   python app.py
# Then open http://localhost:5001 in your browser.
#
# Routes:
#   GET  /                      → serve the UI (index.html)
#   POST /run                   → upload CSV, start background job, return job_id
#   GET  /progress/<job_id>     → return live JSON progress for a job
#   GET  /download/<job_id>     → download the finished output CSV

import csv
import io
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request, send_file

import config
from main import run_pipeline

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def _update_job(job_id: str, **kwargs):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the main UI page."""
    default_key = config.ANTHROPIC_API_KEY or ""
    return render_template("index.html", default_api_key=default_key)


@app.route("/run", methods=["POST"])
def run():
    """
    Accept a CSV upload, validate it, and start a background processing thread.
    Returns JSON: {job_id, total} or {error}
    """
    # --- Validate file upload ---
    if "csv_file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["csv_file"]
    if not f.filename or not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "Please upload a .csv file"}), 400

    # --- Read settings ---
    api_key   = request.form.get("api_key",   "").strip()
    try:
        row_limit = int(request.form.get("row_limit", 0))
    except (ValueError, TypeError):
        row_limit = 0

    # --- Parse CSV ---
    try:
        content = f.stream.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        rows = [dict(row) for row in reader]
    except Exception as e:
        return jsonify({"error": f"Could not read CSV: {e}"}), 400

    if not rows:
        return jsonify({"error": "The uploaded CSV is empty"}), 400

    # Apply row limit (0 or missing = process all rows)
    if row_limit and row_limit > 0:
        rows = rows[:row_limit]

    sample_keys = set(rows[0].keys())
    if "company_name" not in sample_keys:
        return jsonify({"error": "CSV must have a 'company_name' column"}), 400
    if "domain" not in sample_keys and "website" not in sample_keys:
        return jsonify({"error": "CSV must have a 'website' or 'domain' column"}), 400

    # --- Create temp output file ---
    output_fd, output_path = tempfile.mkstemp(suffix=".csv", prefix="wpf_output_")
    os.close(output_fd)

    # --- Register job ---
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status":     "running",
            "current":    0,
            "total":      len(rows),
            "company":    "",
            "analyzed":   0,
            "failed":     0,
            "output_path": output_path,
            "error":      "",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

    # --- Start background thread ---
    def background_job():
        def progress_callback(current, total, company_name, analyzed, failed):
            _update_job(job_id,
                        current=current,
                        total=total,
                        company=company_name,
                        analyzed=analyzed,
                        failed=failed)

        try:
            run_pipeline(
                rows=rows,
                output_path=output_path,
                api_key=api_key,
                progress_callback=progress_callback,
            )
            _update_job(job_id, status="done")
        except Exception as e:
            _update_job(job_id, status="error", error=str(e))

    thread = threading.Thread(target=background_job, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "total": len(rows)})


@app.route("/progress/<job_id>")
def progress(job_id: str):
    """Return current job progress as JSON."""
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "status":   job["status"],
        "current":  job["current"],
        "total":    job["total"],
        "company":  job["company"],
        "analyzed": job["analyzed"],
        "failed":   job["failed"],
        "error":    job["error"],
    })


@app.route("/download/<job_id>")
def download(job_id: str):
    """Return the finished output CSV as a file download."""
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] != "done":
        return jsonify({"error": "Job is not finished yet"}), 400

    output_path = job["output_path"]
    if not os.path.exists(output_path):
        return jsonify({"error": "Output file not found"}), 500

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"website_problems_{timestamp}.csv"

    return send_file(
        output_path,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 55)
    print("  Website Problem Finder")
    print("  Open your browser at: http://localhost:5001")
    print("=" * 55)
    app.run(debug=False, host="0.0.0.0", port=5002)
