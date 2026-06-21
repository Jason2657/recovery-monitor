"""
Web research module for PostCare AI Monitor.

Sources (priority order):
  1. PubMed E-utilities API  — peer-reviewed, always available, disease name in every abstract
  2. Browserbase + Playwright — cloud browser for PubMed search UI + Cleveland Clinic search
  3. NIH MedlinePlus          — reliable direct fetch, disease-specific pages
  4. Cleveland Clinic          — direct URL fallback if Browserbase unavailable
  5. NHLBI                    — additional NIH source for cardiac/pulmonary conditions
"""

import asyncio
import re
import json
import logging
import urllib.parse
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

import os
_LOG = logging.getLogger("web_research")
if not _LOG.handlers:
    fh = logging.FileHandler("/tmp/postcareai_research.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _LOG.addHandler(fh)
    _LOG.setLevel(logging.DEBUG)

MODEL = "claude-opus-4-8"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Condition config — disease_name must appear in every piece of fetched content
_CONDITIONS: dict = {
    "chf": {
        "disease_name": "heart failure",
        "pubmed_queries": [
            "heart failure post-discharge readmission prevention remote monitoring",
            "congestive heart failure complications early warning signs management",
            "acute decompensated heart failure 30 day readmission outcomes",
        ],
        "bb_pubmed_query": "heart failure post discharge readmission risk",
        "bb_cleveland_query": "heart failure recovery after discharge complications",
        "cc_direct_url": "https://my.clevelandclinic.org/health/diseases/17069-heart-failure-understanding-heart-failure",
        "ml_url": "https://medlineplus.gov/heartfailure.html",
        "nhlbi_url": "https://www.nhlbi.nih.gov/health/heart-failure",
    },
    "copd": {
        "disease_name": "COPD",
        "pubmed_queries": [
            "COPD exacerbation readmission prevention remote monitoring post-discharge",
            "chronic obstructive pulmonary disease complications early detection home monitoring",
            "COPD acute exacerbation 30-day hospital readmission risk factors",
        ],
        "bb_pubmed_query": "COPD exacerbation post discharge readmission prevention",
        "bb_cleveland_query": "COPD exacerbation recovery management",
        "cc_direct_url": "https://my.clevelandclinic.org/health/diseases/8709-chronic-obstructive-pulmonary-disease-copd",
        "ml_url": "https://medlineplus.gov/copd.html",
        "nhlbi_url": "https://www.nhlbi.nih.gov/health/copd",
    },
    "post_surgical": {
        "disease_name": "knee replacement",
        "pubmed_queries": [
            "total knee arthroplasty complications readmission post-discharge monitoring",
            "knee replacement recovery remote monitoring surgical site infection",
            "total knee replacement 30-day readmission risk factors outcomes",
        ],
        "bb_pubmed_query": "total knee arthroplasty post discharge complications readmission",
        "bb_cleveland_query": "knee replacement recovery complications warning signs",
        "cc_direct_url": "https://my.clevelandclinic.org/health/treatments/8619-knee-replacement",
        "ml_url": "https://medlineplus.gov/kneereplacement.html",
        "nhlbi_url": None,
    },
    "general": {
        "disease_name": "post-surgical recovery",
        "pubmed_queries": [
            "post-discharge remote patient monitoring hospital readmission prevention",
        ],
        "bb_pubmed_query": "post discharge patient monitoring readmission prevention",
        "bb_cleveland_query": "hospital discharge recovery monitoring",
        "cc_direct_url": None,
        "ml_url": "https://medlineplus.gov/dischargeplanning.html",
        "nhlbi_url": None,
    },
}


# ── Utilities ─────────────────────────────────────────────────────────────────

def _parse_json_safe(text: str) -> dict:
    """Parse JSON from Claude response; inline to avoid circular imports."""
    if not text:
        return {}
    clean = text.strip()
    # Strip markdown fences
    clean = re.sub(r'^```(?:json)?\s*\n?', '', clean)
    clean = re.sub(r'\n?```\s*$', '', clean).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]+\}', clean)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return {}


def _extract_main_content(html: str, max_chars: int = 6000) -> str:
    """Extract article/main content from HTML, skipping nav/footer/sidebar noise."""
    if not html:
        return ""
    if BS4_AVAILABLE:
        soup = BeautifulSoup(html, "html.parser")
        # First pass: remove structural noise tags
        for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                          "form", "button", "noscript", "iframe", "svg", "input"]):
            tag.decompose()
        # Second pass: collect nav-ish elements by class/id BEFORE decomposing
        # (avoid mutating while iterating — collect first, then remove)
        nav_pat = re.compile(r"nav|menu|sidebar|breadcrumb|cookie|modal|popup|banner|ad-", re.I)
        to_remove = []
        for tag in soup.find_all(True):
            try:
                attrs = tag.attrs
                if attrs is None:
                    continue
                cl = " ".join(attrs.get("class") or []) + " " + (attrs.get("id") or "")
                if nav_pat.search(cl):
                    to_remove.append(tag)
            except Exception:
                continue
        for tag in to_remove:
            try:
                tag.decompose()
            except Exception:
                pass
        # Prefer semantic main-content elements
        main = (
            soup.find("main") or
            soup.find("article") or
            soup.find(id=re.compile(r"content|article|main-body", re.I)) or
            soup.find(class_=re.compile(r"^(content|article|body|prose|entry)", re.I)) or
            soup.body or soup
        )
        text = (main or soup).get_text(separator=" ", strip=True)
    else:
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        for ent, ch in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&nbsp;"," "),("&quot;",'"')]:
            text = text.replace(ent, ch)
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


# ── PubMed E-utilities (primary, always available) ────────────────────────────

async def _pubmed_api(queries: list[str], disease_name: str, max_results: int = 4) -> list[dict]:
    """Search PubMed via E-utilities; verify disease name appears in each abstract."""
    results = []
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=25.0) as c:
        for query in queries:
            if len(results) >= max_results:
                break
            try:
                sr = await c.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params={"db": "pubmed", "term": query, "retmax": 3,
                            "retmode": "json", "sort": "relevance"},
                )
                ids = [i for i in sr.json().get("esearchresult", {}).get("idlist", [])
                       if i not in seen]
                if not ids:
                    continue
                await asyncio.sleep(0.4)
                fr = await c.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params={"db": "pubmed", "id": ",".join(ids[:3]),
                            "rettype": "abstract", "retmode": "text"},
                )
                raw = "\n" + fr.text.strip()
                blocks = re.split(r"\n\d+\. ", raw)
                for idx, block in enumerate(blocks[1:], 0):
                    block = block.strip()
                    if len(block) < 80 or idx >= len(ids):
                        continue
                    pmid = ids[idx]
                    if pmid in seen:
                        continue
                    # Enforce quality: disease name must appear in the text
                    if disease_name.lower() not in block.lower():
                        continue
                    seen.add(pmid)
                    results.append({
                        "pmid": pmid,
                        "text": block[:2500],
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    })
            except Exception as e:
                _LOG.warning(f"PubMed API query failed ({query!r}): {e}")
    return results[:max_results]


# ── Browserbase helpers ───────────────────────────────────────────────────────

async def _bb_create_session(bb_key: str, bb_project: str) -> str:
    """Create a Browserbase session and return the session ID."""
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            "https://api.browserbase.com/v1/sessions",
            headers={"X-BB-API-Key": bb_key, "Content-Type": "application/json"},
            json={"projectId": bb_project},
        )
        if r.status_code in (200, 201):
            return r.json().get("id", "")
    return ""


async def _bb_search_pubmed(query: str, bb_key: str, bb_project: str) -> list[str]:
    """
    Use Browserbase + Playwright to search PubMed and return up to 5 PMIDs.
    Falls back silently to [].
    """
    if not (PLAYWRIGHT_AVAILABLE and bb_key and bb_project):
        return []
    session_id = await _bb_create_session(bb_key, bb_project)
    if not session_id:
        return []
    encoded = urllib.parse.quote_plus(query)
    url = f"https://pubmed.ncbi.nlm.nih.gov/?term={encoded}&sort=relevance"
    try:
        cdp = f"wss://connect.browserbase.com?apiKey={bb_key}&sessionId={session_id}"
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait for JS-rendered search results
            try:
                await page.wait_for_selector("[data-article-id]", timeout=8000)
            except Exception:
                await page.wait_for_timeout(3000)
            html = await page.content()
            await browser.close()

        # Extract PMIDs from search results page
        pmids = []
        if BS4_AVAILABLE:
            soup = BeautifulSoup(html, "html.parser")
            for el in soup.select("[data-article-id]"):
                pmid = el.get("data-article-id", "")
                if pmid and pmid.isdigit() and pmid not in pmids:
                    pmids.append(pmid)
            if not pmids:
                for a in soup.select("a[href]"):
                    m = re.search(r'/(\d{7,8})/?$', a.get("href", ""))
                    if m and m.group(1) not in pmids:
                        pmids.append(m.group(1))
        else:
            pmids = list(dict.fromkeys(re.findall(r'data-article-id="(\d+)"', html)))
        _LOG.info(f"Browserbase PubMed search found PMIDs: {pmids[:5]}")
        return pmids[:5]
    except Exception as e:
        _LOG.warning(f"Browserbase PubMed search failed: {e}")
        return []


async def _bb_fetch_cleveland(disease_query: str, bb_key: str, bb_project: str) -> str:
    """
    Use Browserbase to search Cleveland Clinic, navigate to top health result,
    and return extracted text content about the disease.
    """
    if not (PLAYWRIGHT_AVAILABLE and bb_key and bb_project):
        return ""
    session_id = await _bb_create_session(bb_key, bb_project)
    if not session_id:
        return ""
    encoded = urllib.parse.quote_plus(disease_query)
    search_url = f"https://my.clevelandclinic.org/search#q={encoded}&t=All"
    try:
        cdp = f"wss://connect.browserbase.com?apiKey={bb_key}&sessionId={session_id}"
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Click the first /health/ link in results
            try:
                links = await page.query_selector_all("a[href*='/health/']")
                for link in links[:5]:
                    href = await link.get_attribute("href") or ""
                    if href and "/health/" in href and "search" not in href:
                        if not href.startswith("http"):
                            href = "https://my.clevelandclinic.org" + href
                        await page.goto(href, wait_until="domcontentloaded", timeout=25000)
                        await page.wait_for_timeout(2000)
                        break
            except Exception:
                pass

            html = await page.content()
            await browser.close()

        content = _extract_main_content(html, max_chars=5000)
        _LOG.info(f"Browserbase Cleveland Clinic content: {len(content)} chars")
        return content if len(content) > 300 else ""
    except Exception as e:
        _LOG.warning(f"Browserbase Cleveland Clinic failed: {e}")
        return ""


# ── Direct HTTP fetch ─────────────────────────────────────────────────────────

async def _fetch_url(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=25.0) as c:
            r = await c.get(url, headers=_HEADERS, follow_redirects=True)
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
    except Exception as e:
        _LOG.warning(f"Direct fetch failed ({url}): {e}")
    return ""


# ── Fallback synthesis ────────────────────────────────────────────────────────

def _fallback_synthesis(sources: list[dict], disease_name: str, diagnosis: str) -> dict:
    """Minimal synthesis when Claude call fails — pulls real sentences from sources."""
    all_text = " ".join(s["text"] for s in sources)
    sentences = re.split(r'(?<=[.!?])\s+', all_text)
    rel = [s.strip() for s in sentences if disease_name.lower() in s.lower() and len(s) > 60][:6]
    context = " ".join(rel[:2]) if rel else f"{diagnosis} requires careful post-discharge monitoring."
    return {
        "diagnosis_context": context,
        "surgery_reason": "",
        "recovery_timeline": {
            "week_1": rel[2] if len(rel) > 2 else "Monitor vital signs daily.",
            "week_2": rel[3] if len(rel) > 3 else "Continue medication adherence.",
            "month_1": rel[4] if len(rel) > 4 else "Follow-up with care team at 30 days.",
        },
        "common_complications": [],
        "monitoring_parameters": [],
        "red_flags": [
            "Sudden worsening of symptoms → Contact care team immediately",
            "Fever >101°F → Contact care team within 4 hours",
        ],
        "evidence_highlights": [
            {
                "source": s["name"], "url": s["url"],
                "key_finding": s["text"][:180],
                "clinical_relevance": f"Evidence on {disease_name} management",
            }
            for s in sources[:3]
        ],
        "tailored_risk_narrative": (
            f"Patient has a diagnosis of {diagnosis}. "
            + (" ".join(rel[3:6]) if len(rel) > 3 else "Monitor closely per care team instructions.")
        ),
    }


# ── Main research function ────────────────────────────────────────────────────

async def research_patient(
    profile: dict,
    anthropic_client,
    bb_key: str = "",
    bb_project: str = "",
    progress_cb: Optional[Callable] = None,
) -> dict:
    """
    Fetch peer-reviewed literature and clinical guidelines for this patient's diagnosis.
    Returns a structured dict injected into the agent pipeline.
    """
    condition  = profile.get("primary_condition", "general")
    diagnosis  = profile.get("diagnosis", "")
    name       = profile.get("name", "patient")
    age        = profile.get("age", "")
    sex        = profile.get("sex", "")
    cfg        = _CONDITIONS.get(condition, _CONDITIONS["general"])
    disease    = cfg["disease_name"]

    # Build PubMed queries — first query always includes the patient's exact diagnosis
    queries = list(cfg["pubmed_queries"])
    diag_short = diagnosis.split("(")[0].strip()[:60]
    if diag_short and diag_short.lower() not in queries[0].lower():
        queries.insert(0, f"{diag_short} post-discharge monitoring complications readmission")

    raw_sources: list[dict] = []

    # ── Step 1: PubMed ──────────────────────────────────────────────────────
    if progress_cb:
        await progress_cb("PubMed", "Searching for peer-reviewed articles…")

    bb_pmids: list[str] = []
    if bb_key and bb_project and PLAYWRIGHT_AVAILABLE:
        if progress_cb:
            await progress_cb("PubMed", f"Browserbase searching pubmed.ncbi.nlm.nih.gov for '{cfg['bb_pubmed_query']}'…")
        bb_pmids = await _bb_search_pubmed(cfg["bb_pubmed_query"], bb_key, bb_project)
        if bb_pmids and progress_cb:
            await progress_cb("PubMed", f"Browserbase found {len(bb_pmids)} articles — fetching abstracts…")

    if bb_pmids:
        # Fetch abstracts for Browserbase-found articles
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                await asyncio.sleep(0.4)
                fr = await c.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params={"db": "pubmed", "id": ",".join(bb_pmids[:4]),
                            "rettype": "abstract", "retmode": "text"},
                )
                raw = "\n" + fr.text.strip()
                blocks = re.split(r"\n\d+\. ", raw)
                added = 0
                for idx, block in enumerate(blocks[1:], 0):
                    block = block.strip()
                    if len(block) > 80 and idx < len(bb_pmids) and disease.lower() in block.lower():
                        raw_sources.append({
                            "type": "journal", "name": "PubMed",
                            "url": f"https://pubmed.ncbi.nlm.nih.gov/{bb_pmids[idx]}/",
                            "text": block[:2500],
                        })
                        added += 1
                if progress_cb:
                    await progress_cb("PubMed", f"Fetched {added} abstracts via Browserbase ✓")
        except Exception as e:
            _LOG.warning(f"PubMed efetch for Browserbase PMIDs failed: {e}")

    # API fallback / supplement to reach at least 2 abstracts
    if len([s for s in raw_sources if s["name"] == "PubMed"]) < 2:
        if progress_cb:
            await progress_cb("PubMed", "Fetching via E-utilities API…")
        api_articles = await _pubmed_api(queries, disease, max_results=4)
        seen_urls = {s["url"] for s in raw_sources}
        added = 0
        for art in api_articles:
            if art["url"] not in seen_urls:
                raw_sources.append({
                    "type": "journal", "name": "PubMed",
                    "url": art["url"], "text": art["text"],
                })
                seen_urls.add(art["url"])
                added += 1
        n_pubmed = len([s for s in raw_sources if s["name"] == "PubMed"])
        if progress_cb:
            await progress_cb("PubMed", f"Found {n_pubmed} article(s) total ✓" if n_pubmed else "Found 0 articles — check queries")

    # ── Step 2: Cleveland Clinic — DISABLED (Browserbase incompatible) ─────────

    # ── Step 3: NIH MedlinePlus ─────────────────────────────────────────────
    if cfg.get("ml_url"):
        if progress_cb:
            await progress_cb("NIH MedlinePlus", "Fetching…")
        html = await _fetch_url(cfg["ml_url"])
        ml_text = _extract_main_content(html, max_chars=5000)
        if len(ml_text) > 300:
            raw_sources.append({
                "type": "clinical", "name": "NIH MedlinePlus",
                "url": cfg["ml_url"], "text": ml_text,
            })
            if progress_cb:
                await progress_cb("NIH MedlinePlus", f"Extracted {len(ml_text):,} chars ✓")
        elif progress_cb:
            await progress_cb("NIH MedlinePlus", "Could not extract — skipped")

    # ── Step 4: NHLBI ───────────────────────────────────────────────────────
    if cfg.get("nhlbi_url"):
        if progress_cb:
            await progress_cb("NHLBI", "Fetching…")
        html = await _fetch_url(cfg["nhlbi_url"])
        nhlbi_text = _extract_main_content(html, max_chars=4000)
        if len(nhlbi_text) > 300:
            raw_sources.append({
                "type": "clinical", "name": "NHLBI",
                "url": cfg["nhlbi_url"], "text": nhlbi_text,
            })
            if progress_cb:
                await progress_cb("NHLBI", f"Extracted {len(nhlbi_text):,} chars ✓")

    if not raw_sources:
        return {
            "error": "No sources returned usable content. Check network and try again.",
            "sources_fetched": [],
            "patient_id": profile.get("id", ""),
            "diagnosis": diagnosis,
        }

    _LOG.info(f"Sources for {profile.get('id')}: {[(s['name'], len(s['text'])) for s in raw_sources]}")

    # ── Step 5: Claude synthesis ────────────────────────────────────────────
    if progress_cb:
        await progress_cb("Claude", f"Synthesizing {len(raw_sources)} sources about {disease}…")

    sources_block = "\n\n---\n\n".join(
        f"SOURCE [{i+1}]: {s['name']}\nURL: {s['url']}\n\n{s['text'][:3000]}"
        for i, s in enumerate(raw_sources[:5])
    )

    system = (
        f"You are a clinical evidence synthesizer. Extract and organize ONLY evidence-based information "
        f"that specifically relates to {disease} from the sources provided. "
        f"Every section of your output must be specific to {disease} — do not include generic recovery information. "
        f"Return ONLY valid JSON. No markdown code fences. No explanation outside the JSON."
    )

    user = f"""Patient: {name} | Age: {age} | Sex: {sex}
Diagnosis: {diagnosis}
Condition type: {condition}

MEDICAL SOURCES (peer-reviewed and clinical, all specific to {disease}):
{sources_block}

Return ONLY this JSON structure. All fields must specifically mention {disease}:
{{
  "diagnosis_context": "2-3 sentences on what {disease} is, why post-discharge monitoring is critical, and the 30-day readmission landscape",
  "surgery_reason": "If this is a surgical case: what pathology or condition necessitated the surgery. Empty string if not surgical.",
  "recovery_timeline": {{
    "week_1": "Expected milestones and {disease}-specific warning signs in week 1 post-discharge",
    "week_2": "Expected milestones and warning signs in week 2",
    "month_1": "Expected state at 30 days and remaining concerns"
  }},
  "common_complications": [
    {{
      "name": "complication specific to {disease}",
      "incidence": "X% of {disease} patients",
      "onset_window": "days X-Y post-discharge",
      "warning_signs": ["sign 1", "sign 2"],
      "monitoring_action": "specific threshold/parameter to watch"
    }}
  ],
  "monitoring_parameters": [
    {{
      "parameter": "measurable parameter",
      "normal_range": "range",
      "concern_threshold": "value that triggers review",
      "evidence_basis": "brief rationale from literature"
    }}
  ],
  "red_flags": [
    "Specific {disease} finding → Specific action (e.g. Weight >5 lbs/week → physician contact within 4h)"
  ],
  "evidence_highlights": [
    {{
      "source": "source name",
      "url": "url",
      "key_finding": "1-2 sentence finding from this source about {disease}",
      "clinical_relevance": "how this applies to {name}"
    }}
  ],
  "tailored_risk_narrative": "2-3 sentences max — key risk factors specific to {name} ({age}yo {sex}) with {diagnosis}. What makes this patient's profile distinct."
}}"""

    synthesis = {}
    raw_response = ""
    try:
        with anthropic_client.messages.stream(
            model=MODEL,
            max_tokens=6000,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()

        raw_response = next(
            (b.text for b in msg.content if hasattr(b, "text") and b.type == "text"),
            ""
        )
        _LOG.info(f"Synthesis raw ({len(raw_response)} chars): {raw_response[:300]!r}")

        synthesis = _parse_json_safe(raw_response)

        if not synthesis.get("diagnosis_context"):
            _LOG.error(f"Synthesis missing diagnosis_context. Keys: {list(synthesis.keys())}. Raw: {raw_response[:400]!r}")
            synthesis = _fallback_synthesis(raw_sources, disease, diagnosis)
            if progress_cb:
                await progress_cb("Claude", "Synthesis JSON incomplete — applied fallback ✓")
        else:
            if progress_cb:
                await progress_cb("Claude", f"Synthesis complete — {len(synthesis.get('red_flags',[]))} red flags, {len(synthesis.get('common_complications',[]))} complications ✓")

    except Exception as e:
        _LOG.error(f"Synthesis exception: {e}. Raw: {raw_response[:300]!r}")
        synthesis = _fallback_synthesis(raw_sources, disease, diagnosis)
        if progress_cb:
            await progress_cb("Claude", f"Synthesis error ({e}) — fallback applied ✓")

    synthesis["sources_fetched"] = [
        {"name": s["name"], "url": s["url"], "type": s["type"]}
        for s in raw_sources
    ]
    synthesis["patient_id"] = profile.get("id", "")
    synthesis["diagnosis"]  = diagnosis
    synthesis["condition"]  = condition
    synthesis["_raw_response_length"] = len(raw_response)

    return synthesis


# ── Compact summary for agent injection ───────────────────────────────────────

def format_for_agents(research: dict) -> str:
    """
    Compact plain-text block for injecting research into agent prompts.
    Caps token usage: max 4 complications, 4 params, 6 red flags, 3 evidence items.
    """
    if not research or research.get("error"):
        return ""

    comps = research.get("common_complications") or []
    comp_lines = "\n".join(
        f"  • {c.get('name','?')} — onset {c.get('onset_window','?')}, "
        f"incidence {c.get('incidence','?')}: "
        + ", ".join(c.get("warning_signs", [])[:2])
        for c in comps[:4]
    )

    params = research.get("monitoring_parameters") or []
    param_lines = "\n".join(
        f"  • {p.get('parameter','?')}: concern ≥ {p.get('concern_threshold','?')} "
        f"({p.get('evidence_basis','')[:80]})"
        for p in params[:4]
    )

    flags = research.get("red_flags") or []
    flag_lines = "\n".join(f"  ⚠ {f}" for f in flags[:6])

    evidence = research.get("evidence_highlights") or []
    evid_lines = "\n".join(
        f"  [{e.get('source','?')}]: {e.get('key_finding','')[:120]}"
        for e in evidence[:3]
    )

    sources = [s.get("name","") for s in (research.get("sources_fetched") or [])]

    return f"""LITERATURE-BASED CLINICAL CONTEXT (sources: {', '.join(sources)})
Diagnosis: {research.get('diagnosis','')}
{research.get('diagnosis_context','')[:400]}

EVIDENCE-BASED COMPLICATIONS:
{comp_lines or '  (none extracted)'}

MONITORING PARAMETERS FROM LITERATURE:
{param_lines or '  (none extracted)'}

RED FLAGS FROM LITERATURE:
{flag_lines or '  (none extracted)'}

KEY EVIDENCE:
{evid_lines or '  (none extracted)'}

PATIENT-SPECIFIC RISK:
{research.get('tailored_risk_narrative','')[:600]}"""
