#!/usr/bin/env python3
"""
PostCare AI Monitor — FastAPI web backend.
Serves the web UI and streams agent analysis via Server-Sent Events (SSE).
Also provides Deepgram STT/TTS endpoints and patient CRUD.

Run: python3 app.py
Then open: http://localhost:8000
"""

import os
import json
import asyncio
import threading
import queue
import random
import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

load_dotenv()

app = FastAPI(title="PostCare AI Monitor")

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ─── In-memory caches ─────────────────────────────────────────────────────────
last_results: dict = {}   # patient_id -> {risk_score, risk_level, timestamp}

# ─── Clients (lazy-init) ──────────────────────────────────────────────────────
_anthropic_client: anthropic.Anthropic | None = None
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")


def get_anthropic_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


# ─── Saved-patient helpers ────────────────────────────────────────────────────

DATA_DIR = "data/patients"


def load_saved_patients() -> dict:
    """Load custom patients previously saved from the web UI."""
    saved = {}
    if not os.path.isdir(DATA_DIR):
        return saved
    for fname in sorted(os.listdir(DATA_DIR)):
        if fname.endswith(".json") and fname.startswith("PT-"):
            try:
                with open(os.path.join(DATA_DIR, fname)) as f:
                    pdata = json.load(f)
                pid = pdata.get("profile", {}).get("id", fname.replace(".json", ""))
                saved[pid] = pdata
            except Exception:
                pass
    return saved


def generate_patient_id() -> str:
    """Generate a unique PT-XXXX ID that doesn't clash with existing patients."""
    os.makedirs(DATA_DIR, exist_ok=True)
    hardcoded = {"7421", "3892", "5163"}
    existing = {
        fname[3:-5]
        for fname in os.listdir(DATA_DIR)
        if fname.startswith("PT-") and fname.endswith(".json")
    }
    taken = hardcoded | existing
    while True:
        num = str(random.randint(1000, 9999))
        if num not in taken:
            return f"PT-{num}"


def all_patients() -> dict:
    """Return hardcoded patients merged with any saved custom patients."""
    from patient_data import PATIENTS
    return {**PATIENTS, **load_saved_patients()}


# ─── Routes: static / status ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    try:
        with open("static/index.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>static/index.html not found</h1>", status_code=500)


@app.get("/api/status")
async def status():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    dg_key = os.environ.get("DEEPGRAM_API_KEY", "")
    return JSONResponse({
        "api_key_set": bool(api_key),
        "deepgram_key_set": bool(dg_key),
        "model": "claude-opus-4-8",
    })


@app.get("/api/mesh")
async def mesh_info():
    """Fetch.ai mesh status — the real uAgent addresses (no network needed)."""
    from mesh import fetch_mesh
    return JSONResponse({
        "available": fetch_mesh.is_available(),
        "agents": fetch_mesh.mesh_status(),  # [{stage, name, address}, ...]
    })


# ─── Routes: patients ─────────────────────────────────────────────────────────

@app.get("/api/patients")
async def get_patients():
    patients = all_patients()
    result = []
    for pid, pdata in patients.items():
        p = dict(pdata["profile"])
        p["id"] = pid
        p["days_post_discharge"] = len(pdata["sensor_history"])
        p["sensor_history"] = pdata["sensor_history"]
        p["latest_self_report"] = pdata["self_reports"][-1]["text"] if pdata["self_reports"] else ""
        p["is_custom"] = pid not in ("PT-7421", "PT-3892", "PT-5163")
        if pid in last_results:
            p["last_risk"] = last_results[pid]
        result.append(p)
    return JSONResponse(result)


@app.get("/api/patient/{patient_id}")
async def get_patient(patient_id: str):
    patients = all_patients()
    if patient_id not in patients:
        raise HTTPException(404, "Patient not found")
    pdata = patients[patient_id]
    payload = dict(pdata["profile"])
    payload["id"] = patient_id
    payload["sensor_history"] = pdata["sensor_history"]
    payload["self_reports"] = pdata["self_reports"]
    payload["days_post_discharge"] = len(pdata["sensor_history"])
    if patient_id in last_results:
        payload["last_risk"] = last_results[patient_id]
    return JSONResponse(payload)


@app.post("/api/patients")
async def create_patient(request: Request):
    """Save a new custom patient created via the web UI."""
    data = await request.json()

    profile = data.get("profile", {})
    if not profile.get("name") or not profile.get("diagnosis"):
        raise HTTPException(400, "name and diagnosis are required")

    sensor_history = data.get("sensor_history", [])
    self_reports = data.get("self_reports", [])

    if not sensor_history:
        raise HTTPException(400, "at least one day of sensor_history is required")

    pid = generate_patient_id()
    profile["id"] = pid

    patient_data = {
        "profile": profile,
        "sensor_history": sensor_history,
        "self_reports": self_reports,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    fpath = os.path.join(DATA_DIR, f"{pid}.json")
    with open(fpath, "w") as f:
        json.dump(patient_data, f, indent=2)

    return JSONResponse({"id": pid, "message": "Patient created successfully"}, status_code=201)


@app.delete("/api/patients/{patient_id}")
async def delete_patient(patient_id: str):
    """Delete a custom patient (only works on user-created patients)."""
    if patient_id in ("PT-7421", "PT-3892", "PT-5163"):
        raise HTTPException(403, "Cannot delete built-in demo patients")
    fpath = os.path.join(DATA_DIR, f"{patient_id}.json")
    if not os.path.exists(fpath):
        raise HTTPException(404, "Patient not found")
    os.remove(fpath)
    last_results.pop(patient_id, None)
    return JSONResponse({"message": "Patient deleted"})


# ─── Routes: Deepgram ─────────────────────────────────────────────────────────

@app.post("/api/transcribe")
async def transcribe_audio(request: Request):
    """
    Receive raw audio from the browser, forward to Deepgram STT, return transcript.
    Content-Type should be whatever MediaRecorder produces (audio/webm, audio/ogg, etc.).
    """
    dg_key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not dg_key:
        raise HTTPException(400, "DEEPGRAM_API_KEY not configured — add it to your .env file")

    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(400, "No audio data received")

    content_type = request.headers.get("content-type", "audio/webm")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.deepgram.com/v1/listen"
            "?model=nova-2&smart_format=true&punctuate=true",
            content=audio_bytes,
            headers={
                "Authorization": f"Token {dg_key}",
                "Content-Type": content_type,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Deepgram STT error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    transcript = (
        data.get("results", {})
            .get("channels", [{}])[0]
            .get("alternatives", [{}])[0]
            .get("transcript", "")
    )
    return JSONResponse({"transcript": transcript, "confidence":
        data.get("results", {})
            .get("channels", [{}])[0]
            .get("alternatives", [{}])[0]
            .get("confidence", None)
    })


@app.post("/api/tts")
async def text_to_speech(request: Request):
    """
    Convert text to speech using Deepgram TTS and stream the audio back.
    Body: {"text": "..."}
    """
    dg_key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not dg_key:
        raise HTTPException(400, "DEEPGRAM_API_KEY not configured — add it to your .env file")

    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")

    # Trim to reasonable length for TTS
    if len(text) > 1000:
        text = text[:1000] + "…"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.deepgram.com/v1/speak?model=aura-asteria-en",
            json={"text": text},
            headers={
                "Authorization": f"Token {dg_key}",
                "Content-Type": "application/json",
            },
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Deepgram TTS error {resp.status_code}: {resp.text[:300]}")

    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "audio/mpeg"),
    )


# ─── Routes: analysis pipeline (SSE) ─────────────────────────────────────────

@app.get("/api/analyze/{patient_id}")
async def analyze(patient_id: str, mesh: bool = False):
    patients = all_patients()
    if patient_id not in patients:
        raise HTTPException(404, "Patient not found")

    try:
        client = get_anthropic_client()
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    patient_data = patients[patient_id]
    event_queue: queue.Queue = queue.Queue()

    def on_start(agent_id: str, label: str):
        event_queue.put({"type": "agent_start", "agent_id": agent_id, "label": label})

    def on_complete(agent_id: str, label: str, brief: dict, elapsed: float):
        event_queue.put({
            "type": "agent_complete",
            "agent_id": agent_id,
            "label": label,
            "brief": brief,
            "elapsed": elapsed,
        })

    def run_pipeline():
        from agents import run_full_pipeline
        try:
            full_log, log_file = run_full_pipeline(
                patient_data, client,
                callbacks={"on_start": on_start, "on_complete": on_complete},
                log_dir="logs",
                use_mesh=mesh,
            )
            last_results[patient_id] = {
                "risk_score": full_log.get("risk_score", 0),
                "risk_level": full_log.get("risk_level", "unknown"),
                "timestamp": full_log.get("analysis_timestamp", ""),
            }
            # Sponsor surfaces: Band governance (gate + audit) and Arize observability.
            event_queue.put({
                "type": "governance",
                "gate": full_log.get("gate_decision", {}),
                "audit": full_log.get("band_audit", []),
                "observability": {
                    "tracing_enabled": full_log.get("tracing_enabled", False),
                    "trace_id": full_log.get("trace_id"),
                    "risk_threshold": full_log.get("risk_threshold"),
                    "self_correction": full_log.get("self_correction", {}),
                },
                "mesh_used": full_log.get("mesh", False),
                "mesh_addresses": full_log.get("mesh_addresses", []),
            })
            event_queue.put({
                "type": "pipeline_complete",
                "risk_score": full_log.get("risk_score", 0),
                "risk_level": full_log.get("risk_level", "unknown"),
                "sbar": full_log.get("sbar", ""),
                "recommended_action": full_log.get("recommended_action", ""),
                "time_sensitivity": full_log.get("time_sensitivity", ""),
                "log_file": log_file,
            })
        except Exception as exc:
            event_queue.put({"type": "error", "message": str(exc)})

    thread = threading.Thread(target=run_pipeline, daemon=True)
    thread.start()

    async def event_stream():
        loop = asyncio.get_event_loop()
        while True:
            item = await loop.run_in_executor(None, event_queue.get)
            yield f"data: {json.dumps(item, default=str)}\n\n"
            if item.get("type") in ("pipeline_complete", "error"):
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Routes: logs ─────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def list_logs():
    os.makedirs("logs", exist_ok=True)
    files = sorted([f for f in os.listdir("logs") if f.endswith(".json")], reverse=True)
    result = []
    for fname in files[:20]:
        try:
            with open(os.path.join("logs", fname)) as f:
                data = json.load(f)
            result.append({
                "filename": fname,
                "patient_id": data.get("patient_id"),
                "patient_name": data.get("patient_name"),
                "timestamp": data.get("analysis_timestamp"),
                "risk_score": data.get("risk_score"),
                "risk_level": data.get("risk_level"),
            })
        except Exception:
            pass
    return JSONResponse(result)


@app.get("/api/logs/{filename}")
async def get_log(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = os.path.join("logs", filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Log not found")
    with open(path) as f:
        return JSONResponse(json.load(f))


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("PostCare AI Monitor")
    print("Web UI → http://localhost:8000")
    print()
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False, log_level="warning")
