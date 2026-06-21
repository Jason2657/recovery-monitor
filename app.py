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
import math
import time
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
DEEPGRAM_API_KEY      = os.environ.get("DEEPGRAM_API_KEY", "")
BROWSERBASE_API_KEY   = os.environ.get("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.environ.get("BROWSERBASE_PROJECT_ID", "")

# research results cache: patient_id -> dict
research_cache: dict = {}


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
        "browserbase_key_set": bool(os.environ.get("BROWSERBASE_API_KEY", "")),
        "model": "claude-opus-4-8",
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


# ─── Routes: live vitals streaming (SSE) ─────────────────────────────────────

@app.get("/api/live/{patient_id}")
async def live_stream(patient_id: str):
    """
    Stream simulated real-time vitals for a patient at 1 Hz.
    Models a wearable + tilt sensor generating live data.
    SSE format: one JSON object per second.
    """
    patients = all_patients()
    if patient_id not in patients:
        raise HTTPException(404, "Patient not found")

    pdata = patients[patient_id]
    profile = pdata["profile"]
    latest = pdata["sensor_history"][-1] if pdata["sensor_history"] else {}
    condition = profile.get("primary_condition", "general")

    base_hr    = float(latest.get("hr_resting_bpm",    profile.get("baseline_hr_bpm",    72)))
    base_spo2  = float(latest.get("spo2_pct",          profile.get("baseline_spo2_pct",  97)))
    base_rr    = float(latest.get("rr_breaths_per_min", 16))
    base_temp  = float(latest.get("temp_c",             37.0))
    base_sbp   = float(latest.get("systolic_bp",        120))
    base_dbp   = float(latest.get("diastolic_bp",        80))
    sleep_ints = int(latest.get("sleep_interruptions",   2))

    # Tilt sensor state machine
    tilt_active   = False
    tilt_count    = 0
    tilt_cooldown = 0
    tilt_timer    = 0

    # HR drift — subtle slow oscillation to simulate activity/rest cycles
    hr_drift = 0.0

    async def generate():
        nonlocal tilt_active, tilt_count, tilt_cooldown, tilt_timer, hr_drift
        t = 0
        while True:
            # Cardiac variability: sine at ~0.1 Hz + Gaussian noise
            hr_drift += random.gauss(0, 0.08)
            hr_drift  = max(-8, min(8, hr_drift))   # bounded drift
            hr = base_hr + hr_drift + 3.0 * math.sin(t * 0.07) + random.gauss(0, 1.2)
            hr = round(max(38, min(200, hr)), 1)

            spo2 = base_spo2 + random.gauss(0, 0.15)
            spo2 = round(max(80, min(100, spo2)), 1)

            rr = base_rr + random.gauss(0, 0.4)
            rr = round(max(6, min(40, rr)), 1)

            temp = base_temp + random.gauss(0, 0.04)
            temp = round(max(34.0, min(42.0, temp)), 2)

            sbp = base_sbp + random.gauss(0, 2.5)
            dbp = base_dbp + random.gauss(0, 1.5)

            # Tilt / sleep-disturbance sensor
            tilt_cooldown = max(0, tilt_cooldown - 1)
            if not tilt_active and tilt_cooldown == 0:
                # Probability per second derived from expected interruptions per night (8h)
                prob_per_sec = sleep_ints / (8 * 3600)
                if random.random() < prob_per_sec * 20:   # ×20 to make demo visible
                    tilt_active = True
                    tilt_count += 1
                    tilt_timer = random.randint(4, 18)
            elif tilt_active:
                tilt_timer -= 1
                if tilt_timer <= 0:
                    tilt_active = False
                    tilt_cooldown = random.randint(45, 180)

            # NEWS2 quick score for live display
            news2 = 0
            if rr <= 8 or rr >= 25: news2 += 3
            elif 9 <= rr <= 11 or 21 <= rr <= 24: news2 += 2 if rr >= 21 else 1
            if spo2 <= 91: news2 += 3
            elif spo2 <= 93: news2 += 2
            elif spo2 <= 95: news2 += 1
            if sbp <= 90 or sbp >= 220: news2 += 3
            elif sbp <= 100: news2 += 2
            elif sbp <= 110: news2 += 1
            if hr <= 40 or hr >= 131: news2 += 3
            elif 111 <= hr <= 130: news2 += 2
            elif (41 <= hr <= 50) or (91 <= hr <= 110): news2 += 1
            if temp <= 35.0 or temp >= 39.1: news2 += (3 if temp <= 35.0 else 2)
            elif 35.1 <= temp <= 36.0 or 38.1 <= temp <= 39.0: news2 += 1

            payload = {
                "t": t,
                "timestamp": time.strftime("%H:%M:%S"),
                "hr":   hr,
                "spo2": spo2,
                "rr":   rr,
                "temp": temp,
                "sbp":  round(sbp, 0),
                "dbp":  round(dbp, 0),
                "tilt_active": tilt_active,
                "tilt_count":  tilt_count,
                "news2_live":  news2,
                "condition":   condition,
            }
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(1)
            t += 1

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Routes: analysis pipeline (SSE) ─────────────────────────────────────────

@app.get("/api/analyze/{patient_id}")
async def analyze(patient_id: str):
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
                web_research=research_cache.get(patient_id),
            )
            last_results[patient_id] = {
                "risk_score": full_log.get("risk_score", 0),
                "risk_level": full_log.get("risk_level", "unknown"),
                "timestamp": full_log.get("analysis_timestamp", ""),
            }
            event_queue.put({
                "type": "pipeline_complete",
                "risk_score": full_log.get("risk_score", 0),
                "risk_level": full_log.get("risk_level", "unknown"),
                "sbar": full_log.get("sbar", ""),
                "recommended_action": full_log.get("recommended_action", ""),
                "time_sensitivity": full_log.get("time_sensitivity", ""),
                "escalation_level": full_log.get("escalation_level", 0),
                "escalation_recommendation": full_log.get("escalation_recommendation", ""),
                "web_research_incorporated": full_log.get("web_research_incorporated", False),
                "web_research_sources": full_log.get("web_research_sources", []),
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


# ─── Routes: web research ─────────────────────────────────────────────────────

@app.get("/api/research/{patient_id}")
async def research(patient_id: str):
    """
    Stream medical web research for a patient via SSE.
    Fetches PubMed articles + Mayo Clinic + Cleveland Clinic pages,
    then synthesizes with Claude into structured clinical knowledge.
    Results are cached in memory.
    """
    patients = all_patients()
    if patient_id not in patients:
        raise HTTPException(404, "Patient not found")

    try:
        client = get_anthropic_client()
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    profile = patients[patient_id]["profile"]
    bb_key     = os.environ.get("BROWSERBASE_API_KEY", "")
    bb_project = os.environ.get("BROWSERBASE_PROJECT_ID", "")

    async def event_stream():
        from web_research import research_patient

        async def progress(source: str, status: str):
            yield f"data: {json.dumps({'type': 'progress', 'source': source, 'status': status}, default=str)}\n\n"

        # We need a generator-friendly callback
        q: asyncio.Queue = asyncio.Queue()

        async def cb(source: str, status: str):
            await q.put({"type": "progress", "source": source, "status": status})

        async def run_research():
            try:
                result = await research_patient(
                    profile=profile,
                    anthropic_client=client,
                    bb_key=bb_key,
                    bb_project=bb_project,
                    progress_cb=cb,
                )
                research_cache[patient_id] = result
                await q.put({"type": "research_complete", "data": result})
            except Exception as e:
                await q.put({"type": "error", "message": str(e)})

        task = asyncio.create_task(run_research())

        while True:
            item = await q.get()
            yield f"data: {json.dumps(item, default=str)}\n\n"
            if item.get("type") in ("research_complete", "error"):
                break

        await task  # ensure cleanup

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/research/{patient_id}/cached")
async def get_cached_research(patient_id: str):
    """Return cached research results without re-fetching."""
    if patient_id not in research_cache:
        raise HTTPException(404, "No research cached for this patient — run /api/research/{id} first")
    return JSONResponse(research_cache[patient_id])


@app.get("/api/research/{patient_id}/debug")
async def debug_research(patient_id: str):
    """Debug endpoint: returns cached research + log tail for diagnosing blank sections."""
    result: dict = {"patient_id": patient_id, "cached": bool(patient_id in research_cache)}
    if patient_id in research_cache:
        r = research_cache[patient_id]
        result["keys_present"] = list(r.keys())
        result["diagnosis_context_preview"] = str(r.get("diagnosis_context", "MISSING"))[:200]
        result["red_flags_count"] = len(r.get("red_flags") or [])
        result["complications_count"] = len(r.get("common_complications") or [])
        result["sources_fetched"] = r.get("sources_fetched", [])
        result["raw_response_length"] = r.get("_raw_response_length", "not recorded")
        result["has_error"] = bool(r.get("error"))
    try:
        with open("/tmp/postcareai_research.log") as f:
            lines = f.readlines()
        result["log_tail"] = "".join(lines[-30:])
    except Exception:
        result["log_tail"] = "log file not found"
    return JSONResponse(result)


# ─── Routes: simplify (plain-language rewrite for patients/families) ──────────

@app.post("/api/simplify")
async def simplify_text(req: Request):
    """
    Rewrite clinical text (SBAR or research summary) in patient-friendly plain English.
    Body: { "text": "...", "type": "sbar"|"research" }
    Returns: { "simplified": "..." }
    """
    body = await req.json()
    text = (body.get("text") or "").strip()
    content_type = body.get("type", "sbar")
    if not text:
        raise HTTPException(400, "text is required")
    try:
        client = get_anthropic_client()
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    if content_type == "sbar":
        system = (
            "You are rewriting a clinical SBAR handoff document so that a patient and their family can fully understand it.\n\n"
            "FORMATTING RULES (follow exactly):\n"
            "- Use these four section headers exactly: **SITUATION**, **BACKGROUND**, **ASSESSMENT**, **RECOMMENDATION**\n"
            "- Do NOT use # or ## markdown headers\n"
            "- RECOMMENDATION items must each be on a single line: '1. Within X hours: action'\n"
            "- Use plain paragraphs inside each section — no sub-headers needed\n\n"
            "LANGUAGE RULES:\n"
            "- No medical abbreviations: 'heart rate' not 'HR', 'blood oxygen level' not 'SpO2', "
            "'blood pressure' not 'BP', 'beats per minute' not 'bpm', 'pounds' not 'lbs'\n"
            "- Explain what numbers mean (e.g. 'a blood oxygen level of 94% — healthy is above 95%')\n"
            "- No Latin or medical jargon — if a term must be used, explain it in plain words immediately after\n"
            "- Warm, honest, non-alarming tone — a worried grandmother should understand every sentence\n"
            "- Keep all the same facts and recommendations, just in accessible language"
        )
    else:
        system = (
            "You are rewriting a medical research summary as a plain-English guide for a patient and their family.\n\n"
            "FORMATTING RULES (follow exactly):\n"
            "- Use a clear title: # [Condition Name]: A Guide for You and Your Family\n"
            "- Use ## for main sections (e.g. ## What This Means, ## What to Watch For)\n"
            "- Use - for bullet lists\n"
            "- Keep the same information structure but in friendly, accessible language\n\n"
            "LANGUAGE RULES:\n"
            "- No medical abbreviations: spell out and briefly explain every term on first use\n"
            "- Replace jargon with everyday words; use simple analogies where they help\n"
            "- Keep a warm, supportive tone — a concerned grandparent should understand every sentence\n"
            "- Convert clinical thresholds to plain language "
            "(e.g. 'weigh yourself every morning — if you gain more than 5 pounds in a week, call your doctor')\n"
            "- Never use percent signs without explaining what they mean in plain language"
        )

    from agents import MODEL
    with client.messages.stream(
        model=MODEL,
        max_tokens=2500,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": f"Rewrite this in patient-friendly language:\n\n{text}"}],
    ) as stream:
        msg = stream.get_final_message()

    simplified = next((b.text for b in msg.content if b.type == "text"), "")
    return JSONResponse({"simplified": simplified})


@app.patch("/api/patients/{patient_id}/self_report")
async def update_self_report(patient_id: str, req: Request):
    """Update the latest self-report text for a patient (day index from request body)."""
    _PROTECTED = {"PT-7421", "PT-3892", "PT-5163"}
    if patient_id in _PROTECTED:
        raise HTTPException(403, "Cannot modify built-in demo patients")
    body = await req.json()
    new_text = (body.get("text") or "").strip()
    if not new_text:
        raise HTTPException(400, "text is required")

    patients = all_patients()
    if patient_id not in patients:
        raise HTTPException(404, "Patient not found")

    patient = patients[patient_id]
    reports = patient.get("self_reports", [])
    if not reports:
        raise HTTPException(400, "No self-reports to update")

    day_index = body.get("day_index", len(reports) - 1)
    if not (0 <= day_index < len(reports)):
        day_index = len(reports) - 1
    reports[day_index]["text"] = new_text
    patient["self_reports"] = reports

    path = os.path.join("data", "patients", f"{patient_id}.json")
    with open(path, "w") as f:
        json.dump(patient, f, indent=2)

    return JSONResponse({"ok": True, "day_index": day_index, "text": new_text})


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
