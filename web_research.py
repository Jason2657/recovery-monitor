"""
Web research module for PostCare AI Monitor.

Sources (in order of reliability):
  - PubMed E-utilities API — peer-reviewed abstracts, free, no key required
  - MedlinePlus (NIH)      — always accessible, clean HTML, authoritative
  - Cleveland Clinic        — accessible with browser headers
  - Browserbase + Playwright — cloud browser for any JS-heavy page (optional)
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

# ── Condition-specific research targets (only sources that actually return content) ──
_TARGETS: dict[str, dict] = {
    "chf": {
        "pubmed": [
            "heart failure readmission prevention remote monitoring",
            "acute decompensated heart failure post-discharge outcomes",
        ],
        "web": [
            ("NIH MedlinePlus",   "https://medlineplus.gov/heartfailure.html"),
            ("Cleveland Clinic",  "https://my.clevelandclinic.org/health/diseases/17069-heart-failure-understanding-heart-failure"),
            ("NHLBI",             "https://www.nhlbi.nih.gov/health/heart-failure"),
        ],
    },
    "copd": {
        "pubmed": [
            "COPD exacerbation remote monitoring readmission prevention",
            "chronic obstructive pulmonary disease post-discharge outcomes",
        ],
        "web": [
            ("NIH MedlinePlus",  "https://medlineplus.gov/copd.html"),
            ("Cleveland Clinic", "https://my.clevelandclinic.org/health/diseases/8709-chronic-obstructive-pulmonary-disease-copd"),
            ("NHLBI",            "https://www.nhlbi.nih.gov/health/copd"),
        ],
    },
    "post_surgical": {
        "pubmed": [
            "total knee arthroplasty complications readmission surgical site infection",
            "knee replacement recovery outcomes remote monitoring",
        ],
        "web": [
            ("NIH MedlinePlus",  "https://medlineplus.gov/kneereplacement.html"),
            ("Cleveland Clinic", "https://my.clevelandclinic.org/health/treatments/8619-knee-replacement"),
            ("AAOS OrthoInfo",   "https://orthoinfo.aaos.org/en/treatment/total-knee-replacement/"),
        ],
    },
    "general": {
        "pubmed": [
            "post-discharge remote patient monitoring readmission prevention",
        ],
        "web": [
            ("NIH MedlinePlus", "https://medlineplus.gov/dischargeplanning.html"),
        ],
    },
}


# ── HTML extraction ────────────────────────────────────────────────────────────

def _extract_text(html: str, max_chars: int = 7000) -> str:
    if not html:
        return ""
    if BS4_AVAILABLE:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                          "form", "button", "noscript", "iframe", "svg"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
    else:
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        for ent, ch in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&nbsp;", " "), ("&quot;", '"'), ("&#39;", "'")]:
            text = text.replace(ent, ch)
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


# ── Fetching helpers ───────────────────────────────────────────────────────────

async def _fetch_direct(url: str, client: httpx.AsyncClient) -> str:
    try:
        resp = await client.get(url, headers=_HEADERS, follow_redirects=True, timeout=25.0)
        if resp.status_code == 200 and len(resp.text) > 500:
            return resp.text
    except Exception:
        pass
    return ""


async def _fetch_browserbase(url: str, bb_key: str, bb_project: str) -> str:
    if not PLAYWRIGHT_AVAILABLE or not bb_key or not bb_project:
        return ""
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(
                "https://api.browserbase.com/v1/sessions",
                headers={"X-BB-API-Key": bb_key, "Content-Type": "application/json"},
                json={"projectId": bb_project},
            )
            if r.status_code not in (200, 201):
                return ""
            session_id = r.json().get("id", "")
        if not session_id:
            return ""

        cdp_url = f"wss://connect.browserbase.com?apiKey={bb_key}&sessionId={session_id}"
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            ctx  = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(1500)
            html = await page.content()
            await browser.close()
            return html
    except Exception:
        return ""


async def _fetch_page(url: str, client: httpx.AsyncClient, bb_key: str, bb_project: str) -> str:
    html = ""
    if bb_key and bb_project and PLAYWRIGHT_AVAILABLE:
        html = await _fetch_browserbase(url, bb_key, bb_project)
    if not html:
        html = await _fetch_direct(url, client)
    return html


# ── PubMed ─────────────────────────────────────────────────────────────────────

async def _pubmed_search(query: str, max_results: int = 4) -> list[dict]:
    results = []
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            # Step 1: search for IDs
            sr = await c.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={"db": "pubmed", "term": query, "retmax": max_results,
                        "retmode": "json", "sort": "relevance"},
            )
            ids = sr.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                return []

            await asyncio.sleep(0.4)   # NCBI rate-limit courtesy

            # Step 2: fetch full abstracts as plain text
            fr = await c.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params={"db": "pubmed", "id": ",".join(ids),
                        "rettype": "abstract", "retmode": "text"},
            )
            raw = fr.text.strip()

            # Normalize: prepend \n so the leading "1. " matches the same pattern as "\n2. "
            normalized = "\n" + raw
            blocks = re.split(r"\n\d+\. ", normalized)
            # blocks[0] is empty (before the first article); blocks[1:] are the articles
            for i, block in enumerate(blocks[1:], 0):
                text = block.strip()
                if len(text) > 100:
                    pmid = ids[i] if i < len(ids) else "?"
                    results.append({
                        "pmid": pmid,
                        "text": text[:2800],
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
    Fetch and synthesize medical research tailored to this patient's specific diagnosis.
    Returns a structured dict; result is meant to be injected into the agent pipeline.
    """
    condition = profile.get("primary_condition", "general")
    diagnosis = profile.get("diagnosis", "")
    name      = profile.get("name", "patient")
    age       = profile.get("age", "")
    sex       = profile.get("sex", "")

    targets = _TARGETS.get(condition, _TARGETS["general"])

    # Always prepend a patient-specific PubMed query derived from their actual diagnosis
    pubmed_queries = list(targets.get("pubmed", []))
    diag_short = diagnosis.split("(")[0].strip()[:70]
    if diag_short:
        pubmed_queries.insert(0, f"{diag_short} post-discharge complications readmission monitoring")

    raw_sources: list[dict] = []

    # ── PubMed ──────────────────────────────────────────────────────────────────
    seen_pmids: set[str] = set()
    for query in pubmed_queries[:2]:
        if progress_cb:
            await progress_cb("PubMed", f"Searching: {query[:65]}…")
        articles = await _pubmed_search(query, max_results=3)
        added = 0
        for art in articles:
            if art["pmid"] not in seen_pmids:
                seen_pmids.add(art["pmid"])
                raw_sources.append({"type": "journal", "name": "PubMed",
                                    "url": art["url"], "pmid": art["pmid"], "text": art["text"]})
                added += 1
        if progress_cb:
            await progress_cb("PubMed", f"Found {added} new articles (total {len(seen_pmids)})")

    # ── Web sources ─────────────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=25.0) as http_client:
        for source_name, url in targets.get("web", []):
            if progress_cb:
                await progress_cb(source_name, "Fetching…")
            html = await _fetch_page(url, http_client, bb_key, bb_project)
            if html:
                text = _extract_text(html)
                if len(text) > 200:    # skip if basically empty after extraction
                    raw_sources.append({"type": "clinical", "name": source_name,
                                        "url": url, "pmid": None, "text": text})
                    if progress_cb:
                        await progress_cb(source_name, f"Extracted {len(text):,} chars ✓")
                else:
                    if progress_cb:
                        await progress_cb(source_name, "Content too short — skipped")
            else:
                if progress_cb:
                    await progress_cb(source_name, "Could not fetch — skipped")

    if not raw_sources:
        return {"error": "No sources returned content. Check network connectivity.",
                "sources_fetched": [], "patient_id": profile.get("id", ""), "diagnosis": diagnosis}

    # ── Claude synthesis ─────────────────────────────────────────────────────────
    if progress_cb:
        await progress_cb("Claude", "Synthesizing research…")

    sources_block = "\n\n---\n\n".join(
        f"SOURCE [{i+1}]: {s['name']}\nURL: {s['url']}\n\n{s['text'][:3500]}"
        for i, s in enumerate(raw_sources)
    )

    system = (
        "You are a clinical evidence synthesizer for a remote patient monitoring AI. "
        "Extract and organize medically accurate, evidence-based information from the sources, "
        "tailored to this patient's specific diagnosis and demographics. "
        "Focus on: post-discharge monitoring parameters, early warning signs, and escalation triggers. "
        "This output will be injected into a multi-agent clinical AI pipeline — be specific, cite sources, "
        "and prioritize actionable thresholds over general information. Return valid JSON only."
    )
    user = f"""Patient: {name} | Age: {age} | Sex: {sex}
Diagnosis: {diagnosis} | Condition type: {condition}

Synthesize the following medical sources into structured clinical knowledge for AI-assisted monitoring:

{sources_block}

Return ONLY valid JSON (no markdown fences):
{{
  "diagnosis_context": "2-3 sentences: what this condition/surgery is, why post-discharge monitoring matters, 30-day readmission landscape",
  "surgery_reason": "If surgical: why this surgery is performed, underlying pathology treated. Empty string otherwise.",
  "recovery_timeline": {{
    "week_1": "Expected milestones and normal vs abnormal findings",
    "week_2": "Expected milestones and normal vs abnormal findings",
    "month_1": "Expected state and typical remaining concerns"
  }},
  "common_complications": [
    {{
      "name": "Complication name",
      "incidence": "X% of patients",
      "onset_window": "Days X-Y post-discharge",
      "warning_signs": ["sign 1", "sign 2"],
      "monitoring_action": "specific parameter and threshold"
    }}
  ],
  "monitoring_parameters": [
    {{
      "parameter": "metric name",
      "normal_range": "range",
      "concern_threshold": "value triggering review",
      "evidence_basis": "brief rationale from literature"
    }}
  ],
  "red_flags": [
    "Specific finding → specific action (e.g. Weight >5 lbs/week → physician contact within 4h)"
  ],
  "evidence_highlights": [
    {{
      "source": "source name",
      "url": "url",
      "key_finding": "1-2 sentence finding",
      "clinical_relevance": "how this applies to this patient"
    }}
  ],
  "tailored_risk_narrative": "2-3 paragraphs specifically about THIS patient's risk profile, what to watch for, and why"
}}"""

    with anthropic_client.messages.stream(
        model=MODEL,
        max_tokens=3000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()

    text_out = next(
        (b.text for b in msg.content if hasattr(b, "text") and b.type == "text"),
        ""
    )

    from agents import _parse_json
    synthesis = _parse_json(text_out)
    synthesis["sources_fetched"] = [
        {"name": s["name"], "url": s["url"], "type": s["type"], "pmid": s.get("pmid")}
        for s in raw_sources
    ]
    synthesis["patient_id"] = profile.get("id", "")
    synthesis["diagnosis"]  = diagnosis
    synthesis["condition"]  = condition
    return synthesis


# ── Compact summary for agent injection ────────────────────────────────────────

def format_for_agents(research: dict) -> str:
    """
    Compact plain-text summary of web research, designed to fit in an agent prompt
    without exceeding token budgets. Called by agents.py.
    """
    if not research or research.get("error"):
        return ""

    comps = research.get("common_complications") or []
    comp_lines = "\n".join(
        f"  • {c.get('name','?')} — onset {c.get('onset_window','?')}, "
        f"incidence {c.get('incidence','?')}: {', '.join(c.get('warning_signs',[])[:2])}"
        for c in comps[:4]
    )

    params = research.get("monitoring_parameters") or []
    param_lines = "\n".join(
        f"  • {p.get('parameter','?')}: concern ≥ {p.get('concern_threshold','?')} "
        f"({p.get('evidence_basis','')})"
        for p in params[:4]
    )

    flags = research.get("red_flags") or []
    flag_lines = "\n".join(f"  ⚠ {f}" for f in flags[:6])

    evidence = research.get("evidence_highlights") or []
    evid_lines = "\n".join(
        f"  [{e.get('source','?')}]: {e.get('key_finding','')}"
        for e in evidence[:3]
    )

    sources = [s.get("name", "") for s in (research.get("sources_fetched") or [])]

    return f"""LITERATURE-BASED CLINICAL CONTEXT (sources: {', '.join(sources)})
Diagnosis: {research.get('diagnosis', '')}
{research.get('diagnosis_context', '')}

EVIDENCE-BASED COMPLICATIONS:
{comp_lines}

MONITORING PARAMETERS FROM LITERATURE:
{param_lines}

RED FLAGS FROM LITERATURE:
{flag_lines}

EVIDENCE HIGHLIGHTS:
{evid_lines}

PATIENT-SPECIFIC RISK NARRATIVE:
{research.get('tailored_risk_narrative', '')[:600]}"""
