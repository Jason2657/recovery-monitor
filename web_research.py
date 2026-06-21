"""
Web research module for PostCare AI Monitor.

Fetches tailored medical literature about a patient's specific condition/surgery from:
  - PubMed E-utilities API (free, no key needed) — peer-reviewed studies
  - Mayo Clinic — patient-facing clinical guides
  - Cleveland Clinic — clinical overviews and recovery protocols

Uses Browserbase + Playwright when BROWSERBASE_API_KEY is configured (better for
JS-heavy pages). Falls back to direct httpx with browser headers otherwise.
"""

import asyncio
import re
import json
import httpx
from typing import Callable, Optional

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

MODEL = "claude-opus-4-8"

# ── Browser-like headers for direct httpx fetches ──────────────────────────────
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ── Condition-specific research targets ────────────────────────────────────────
_TARGETS = {
    "chf": {
        "pubmed": [
            "heart failure 30-day readmission remote patient monitoring weight gain early detection",
            "acute decompensated heart failure post-discharge wearable sensor prediction complication",
        ],
        "web": [
            ("Mayo Clinic", "https://www.mayoclinic.org/diseases-conditions/heart-failure/diagnosis-treatment/drc-20373148"),
            ("Cleveland Clinic", "https://my.clevelandclinic.org/health/diseases/17069-heart-failure-understanding-heart-failure"),
        ],
    },
    "copd": {
        "pubmed": [
            "COPD exacerbation remote monitoring pulse oximetry 30-day readmission prevention",
            "chronic obstructive pulmonary disease post-discharge complication respiratory failure",
        ],
        "web": [
            ("Mayo Clinic", "https://www.mayoclinic.org/diseases-conditions/copd/diagnosis-treatment/drc-20353685"),
            ("Cleveland Clinic", "https://my.clevelandclinic.org/health/diseases/8709-chronic-obstructive-pulmonary-disease-copd"),
        ],
    },
    "post_surgical": {
        "pubmed": [
            "total knee arthroplasty TKA complications surgical site infection readmission 30-day",
            "knee replacement post-discharge recovery activity monitoring remote outcomes",
        ],
        "web": [
            ("Mayo Clinic", "https://www.mayoclinic.org/tests-procedures/knee-replacement/about/pac-20385276"),
            ("Cleveland Clinic", "https://my.clevelandclinic.org/health/treatments/8619-knee-replacement"),
        ],
    },
    "general": {
        "pubmed": [
            "post-discharge remote patient monitoring readmission prevention wearable sensor",
        ],
        "web": [],
    },
}


# ── HTML text extraction ───────────────────────────────────────────────────────

def _extract_text(html: str, max_chars: int = 6000) -> str:
    if not html:
        return ""
    if BS4_AVAILABLE:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "button"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
    else:
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        for ent, ch in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "), ("&quot;", '"'), ("&#39;", "'")]:
            text = text.replace(ent, ch)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


# ── Fetching helpers ───────────────────────────────────────────────────────────

async def _fetch_direct(url: str, client: httpx.AsyncClient) -> str:
    """Direct httpx fetch with browser headers."""
    try:
        resp = await client.get(url, headers=_HEADERS, follow_redirects=True, timeout=15.0)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return ""


async def _fetch_browserbase(url: str, bb_key: str, bb_project: str) -> str:
    """Fetch a JS-rendered page using Browserbase cloud browser + Playwright."""
    if not PLAYWRIGHT_AVAILABLE or not bb_key or not bb_project:
        return ""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.browserbase.com/v1/sessions",
                headers={"X-BB-API-Key": bb_key, "Content-Type": "application/json"},
                json={"projectId": bb_project},
            )
            if resp.status_code not in (200, 201):
                return ""
            session_id = resp.json().get("id", "")
        if not session_id:
            return ""

        cdp_url = f"wss://connect.browserbase.com?apiKey={bb_key}&sessionId={session_id}"
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(1500)
            html = await page.content()
            await browser.close()
            return html
    except Exception:
        return ""


async def _fetch_page(
    url: str,
    source_name: str,
    http_client: httpx.AsyncClient,
    bb_key: str,
    bb_project: str,
) -> str:
    """Fetch a page, preferring Browserbase if configured, else direct."""
    html = ""
    if bb_key and bb_project and PLAYWRIGHT_AVAILABLE:
        html = await _fetch_browserbase(url, bb_key, bb_project)
    if not html:
        html = await _fetch_direct(url, http_client)
    return html


# ── PubMed ─────────────────────────────────────────────────────────────────────

async def _pubmed_search(query: str, max_results: int = 4) -> list[dict]:
    """Search PubMed and return article abstracts."""
    results = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            sr = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={
                    "db": "pubmed", "term": query,
                    "retmax": max_results, "retmode": "json",
                    "sort": "relevance", "datetype": "pdat", "reldate": 1825,  # last 5 years
                },
            )
            ids = sr.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                return []

            await asyncio.sleep(0.35)  # NCBI rate limit courtesy

            fr = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params={"db": "pubmed", "id": ",".join(ids), "rettype": "abstract", "retmode": "text"},
            )
            raw = fr.text
            blocks = re.split(r"\n\d+\. ", raw)
            for i, block in enumerate(blocks[1:], 0):
                text = block.strip()
                if len(text) > 120:
                    pmid = ids[i] if i < len(ids) else "?"
                    results.append({
                        "pmid": pmid,
                        "text": text[:2500],
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    })
    except Exception:
        pass
    return results


# ── Main research function ─────────────────────────────────────────────────────

async def research_patient(
    profile: dict,
    anthropic_client,
    bb_key: str = "",
    bb_project: str = "",
    progress_cb: Optional[Callable] = None,
) -> dict:
    """
    Fetch medical research tailored to this patient's specific diagnosis/surgery.
    Returns a structured dict with synthesis, sources, complications, and red flags.

    progress_cb: async callable(source_name: str, status: str)
    """
    condition = profile.get("primary_condition", "general")
    diagnosis = profile.get("diagnosis", "")
    name = profile.get("name", "patient")
    age = profile.get("age", "")
    sex = profile.get("sex", "")

    targets = _TARGETS.get(condition, _TARGETS["general"])
    pubmed_queries = list(targets.get("pubmed", []))

    # Inject a diagnosis-specific PubMed query for tailored results
    diag_short = diagnosis.split("(")[0].strip()[:70]
    if diag_short:
        pubmed_queries.insert(0,
            f"{diag_short} post-discharge complications readmission 30-day monitoring"
        )

    raw_sources = []

    # ── PubMed ──────────────────────────────────────────────────────────────────
    for query in pubmed_queries[:2]:
        if progress_cb:
            await progress_cb("PubMed", f"Searching: {query[:65]}…")
        articles = await _pubmed_search(query, max_results=3)
        for art in articles:
            raw_sources.append({
                "type": "journal",
                "name": "PubMed",
                "url": art["url"],
                "pmid": art["pmid"],
                "text": art["text"],
            })
        if progress_cb:
            await progress_cb("PubMed", f"Found {len(articles)} articles")

    # ── Web sources ─────────────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=15.0) as http_client:
        for source_name, url in targets.get("web", []):
            if progress_cb:
                await progress_cb(source_name, "Fetching page…")
            html = await _fetch_page(url, source_name, http_client, bb_key, bb_project)
            if html:
                text = _extract_text(html)
                raw_sources.append({
                    "type": "clinical",
                    "name": source_name,
                    "url": url,
                    "pmid": None,
                    "text": text,
                })
                if progress_cb:
                    await progress_cb(source_name, f"Extracted {len(text)} chars")
            else:
                if progress_cb:
                    await progress_cb(source_name, "Could not fetch — skipped")

    if not raw_sources:
        return {
            "error": "No sources could be fetched. Check network connectivity.",
            "sources_fetched": [],
            "patient_id": profile.get("id", ""),
            "diagnosis": diagnosis,
        }

    # ── Claude synthesis ─────────────────────────────────────────────────────────
    if progress_cb:
        await progress_cb("Claude", "Synthesizing research into clinical knowledge…")

    sources_block = "\n\n---\n\n".join([
        f"SOURCE [{i+1}]: {s['name']}\nURL: {s['url']}\n\n{s['text'][:3500]}"
        for i, s in enumerate(raw_sources)
    ])

    system = (
        "You are a clinical evidence synthesizer for a remote patient monitoring AI system. "
        "Your job is to extract and structure medically accurate, evidence-based information "
        "from the provided sources, tailored to a specific patient's diagnosis and demographics. "
        "Focus on post-discharge monitoring parameters, early warning signs of complications, "
        "and evidence-based escalation triggers. Do NOT give advice directly to patients — "
        "this output is for a clinical AI monitoring system. Return valid JSON only."
    )
    user = f"""Patient context:
  Name: {name} | Age: {age} | Sex: {sex}
  Diagnosis: {diagnosis}
  Condition type: {condition}

Synthesize the following medical literature and clinical sources into structured knowledge
for this patient's post-discharge monitoring. Tailor insights to this specific patient.

{sources_block}

Return ONLY valid JSON:
{{
  "diagnosis_context": "2-3 sentences on what this condition/surgery is, why post-discharge monitoring matters, and the 30-day readmission landscape",
  "surgery_reason": "If surgical: 1-2 sentences on the typical reasons this surgery is performed and the underlying pathology being treated",
  "recovery_timeline": {{
    "week_1": "Expected milestones and normal vs abnormal signs",
    "week_2": "Expected milestones and normal vs abnormal signs",
    "month_1": "Expected state and typical remaining concerns"
  }},
  "common_complications": [
    {{
      "name": "Complication name",
      "incidence": "X% of patients",
      "onset_window": "Days X-Y post-discharge",
      "warning_signs": ["sign 1", "sign 2"],
      "monitoring_action": "what to monitor and at what threshold"
    }}
  ],
  "monitoring_parameters": [
    {{
      "parameter": "Metric name",
      "normal_range": "range",
      "concern_threshold": "value that should trigger review",
      "evidence_basis": "brief rationale from the literature"
    }}
  ],
  "red_flags": [
    "Specific sign → specific escalation action (e.g. Weight gain >5 lbs/week → physician contact within 4h)"
  ],
  "evidence_highlights": [
    {{
      "source": "source name",
      "url": "url",
      "key_finding": "1-2 sentence key finding",
      "clinical_relevance": "how this specifically applies to this patient"
    }}
  ],
  "tailored_risk_narrative": "2-3 paragraph narrative specifically about THIS patient's risk profile, what to watch for, and why, based on their diagnosis and condition type"
}}"""

    with anthropic_client.messages.stream(
        model=MODEL,
        max_tokens=3000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()

    text_out = ""
    for block in msg.content:
        if hasattr(block, "text") and block.type == "text":
            text_out = block.text
            break

    # Parse JSON — reuse agents._parse_json
    from agents import _parse_json
    synthesis = _parse_json(text_out)

    synthesis["sources_fetched"] = [
        {"name": s["name"], "url": s["url"], "type": s["type"],
         "pmid": s.get("pmid")}
        for s in raw_sources
    ]
    synthesis["patient_id"] = profile.get("id", "")
    synthesis["diagnosis"] = diagnosis
    synthesis["condition"] = condition

    return synthesis
