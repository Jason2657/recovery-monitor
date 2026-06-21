"""
Seven-agent pipeline for post-discharge readmission monitoring.
Each agent has a distinct clinical role; outputs feed forward through the pipeline.
"""

import json
import re
import time
import os
import datetime
import concurrent.futures
import anthropic

from patient_data import CLINICAL_KNOWLEDGE_BASE

# Sponsor layers (additive; each degrades gracefully if its deps are absent).
import config
from config import MODEL
from observability import tracing, evaluator
from band.adapter import get_band_room
from band.audit import AuditLog


# ─── API helpers ──────────────────────────────────────────────────────────────

def _extract_text(message) -> str:
    for block in message.content:
        if block.type == "text":
            return block.text
    return ""


def _call(client: anthropic.Anthropic, system: str, user: str, max_tokens: int = 3000) -> str:
    with client.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        return _extract_text(stream.get_final_message())


def _parse_json(text: str) -> dict:
    clean = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\s*```\s*$", "", clean, flags=re.MULTILINE).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {"summary": text[:400] if text else "parse error", "severity": "unknown", "_parse_error": True}


# ─── Numerical pre-computation ────────────────────────────────────────────────

def compute_news2(readings: dict) -> dict:
    score = 0
    components = {}

    rr = readings["rr_breaths_per_min"]
    rr_s = 3 if rr <= 8 else (1 if rr <= 11 else (0 if rr <= 20 else (2 if rr <= 24 else 3)))
    score += rr_s
    components["respiratory_rate"] = {"value": rr, "score": rr_s}

    spo2 = readings["spo2_pct"]
    spo2_s = 3 if spo2 <= 91 else (2 if spo2 <= 93 else (1 if spo2 <= 95 else 0))
    score += spo2_s
    components["spo2"] = {"value": spo2, "score": spo2_s}

    sbp = readings["systolic_bp"]
    sbp_s = 3 if sbp <= 90 else (2 if sbp <= 100 else (1 if sbp <= 110 else (0 if sbp <= 219 else 3)))
    score += sbp_s
    components["systolic_bp"] = {"value": sbp, "score": sbp_s}

    hr = readings["hr_resting_bpm"]
    hr_s = 3 if hr <= 40 else (1 if hr <= 50 else (0 if hr <= 90 else (1 if hr <= 110 else (2 if hr <= 130 else 3))))
    score += hr_s
    components["heart_rate"] = {"value": hr, "score": hr_s}

    temp = readings["temp_c"]
    temp_s = 3 if temp <= 35.0 else (1 if temp <= 36.0 else (0 if temp <= 38.0 else (1 if temp <= 39.0 else 2)))
    score += temp_s
    components["temperature"] = {"value": temp, "score": temp_s}

    any_3 = any(c["score"] == 3 for c in components.values())
    if score <= 0:
        risk = "Low"
    elif score <= 4 and not any_3:
        risk = "Low-Medium"
    elif score <= 6 and not any_3:
        risk = "Medium"
    elif score <= 6 or any_3:
        risk = "Medium"
    else:
        risk = "High"

    return {"total": score, "risk": risk, "components": components, "any_single_3": any_3}


def linear_slope(values: list) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return round(num / den, 3) if den != 0 else 0.0


def compute_trends(history: list) -> dict:
    hr = [d["hr_resting_bpm"] for d in history]
    spo2 = [d["spo2_pct"] for d in history]
    weight = [d["weight_lbs"] for d in history]
    steps = [d["steps"] for d in history]
    wakes = [d["sleep_interruptions"] for d in history]
    rr = [d["rr_breaths_per_min"] for d in history]
    sbp = [d["systolic_bp"] for d in history]
    temp = [d["temp_c"] for d in history]

    peak_steps = max(steps)
    curr_steps = steps[-1]

    return {
        "hr_slope_bpm_per_day": linear_slope(hr),
        "hr_range": f"{min(hr)}-{max(hr)} bpm",
        "spo2_slope_pct_per_day": linear_slope(spo2),
        "spo2_range": f"{min(spo2)}-{max(spo2)}%",
        "weight_change_lbs_7days": round(weight[-1] - weight[0], 1),
        "weight_slope_lbs_per_day": linear_slope(weight),
        "steps_peak": peak_steps,
        "steps_current": curr_steps,
        "steps_decline_pct": round((peak_steps - curr_steps) / peak_steps * 100, 1) if peak_steps > 0 else 0,
        "steps_slope_per_day": round(linear_slope(steps)),
        "sleep_wakes_slope_per_day": linear_slope(wakes),
        "sleep_wakes_range": f"{min(wakes)}-{max(wakes)} per night",
        "rr_slope_per_day": linear_slope(rr),
        "sbp_slope_mmhg_per_day": linear_slope(sbp),
        "temp_slope_per_day": linear_slope(temp),
        "temp_range": f"{min(temp)}-{max(temp)}°C",
    }


# ─── Agents 1-7 ───────────────────────────────────────────────────────────────

def run_signal_agent(current_readings: dict, patient_profile: dict, client) -> dict:
    news2 = compute_news2(current_readings)
    condition = patient_profile.get("primary_condition", "general")

    system = (
        "You are a clinical signal monitoring agent in a post-discharge surveillance system. "
        "Analyze vital signs and flag acute anomalies. Be clinically precise — cite specific thresholds. "
        "Tailor your interpretation to the patient's primary condition (CHF, post-surgical, COPD, etc.). "
        "Focus ONLY on live physiological signals: HR, SpO2, respiratory rate, temperature, blood pressure, "
        "and sleep/movement data. Do NOT flag weight or fluid retention findings in acute_alerts or "
        "borderline_concerns — weight trends belong to the Trend Agent. "
        "The summary should be a concise interpretation of the current live vitals and sleep data only. "
        "Return valid JSON only."
    )
    # Pull live sensor values if provided (from the SSE stream feed)
    live = current_readings.get("_live", {})
    live_hr    = live.get("hr",   current_readings.get("hr_resting_bpm", "N/A"))
    live_spo2  = live.get("spo2", current_readings.get("spo2_pct", "N/A"))
    live_rr    = live.get("rr",   current_readings.get("rr_breaths_per_min", "N/A"))
    live_temp  = live.get("temp", current_readings.get("temp_c", "N/A"))
    live_sbp   = live.get("sbp",  current_readings.get("systolic_bp", "N/A"))
    live_dbp   = live.get("dbp",  current_readings.get("diastolic_bp", "N/A"))
    live_label = "(live sensor)" if live else "(last recorded)"

    user = f"""Analyze Day {current_readings['day']} vital signs for:
Patient: {patient_profile['name']}, {patient_profile['age']}yo {patient_profile['sex']}
Diagnosis: {patient_profile['diagnosis']}
Primary condition context: {condition}
Medications: {', '.join(patient_profile.get('medications', []))}
Baseline HR: {patient_profile['baseline_hr_bpm']} bpm | Baseline SpO2: {patient_profile['baseline_spo2_pct']}%

CURRENT VITALS {live_label}:
  HR (resting): {live_hr} bpm
  SpO2: {live_spo2}%
  Respiratory rate: {live_rr} breaths/min
  Temperature: {live_temp}°C
  Blood pressure: {live_sbp}/{live_dbp} mmHg
  Weight: {current_readings['weight_lbs']} lbs (baseline {patient_profile['baseline_weight_lbs']} lbs)
  Steps: {current_readings['steps']}

SLEEP DATA (nighttime sensor):
  Sleep interruptions (recorded): {current_readings['sleep_interruptions']}
  Light-on wake events (live photoresistor): {current_readings.get('light_wake_count', 0)}
  Note: light >200 lux = dark/asleep; ≤200 lux = light on/awake. Wakes counted only after first dark period.

PRE-COMPUTED NEWS2:
  Total: {news2['total']}/15 — {news2['risk']} Risk
  Components: {json.dumps(news2['components'])}
  Any single parameter scored 3: {news2['any_single_3']}

Return JSON ONLY:
{{
  "news2_score": {news2['total']},
  "news2_risk_level": "{news2['risk']}",
  "acute_alerts": ["values crossing defined clinical thresholds — cite the threshold"],
  "borderline_concerns": ["values within normal but approaching a threshold"],
  "summary": "2-3 sentence clinical snapshot",
  "severity": "low|medium|high|critical"
}}"""

    result = _parse_json(_call(client, system, user, max_tokens=1500))
    result["_news2"] = news2
    result["_vitals"] = {
        "hr":   live_hr,
        "rr":   live_rr,
        "sbp":  live_sbp,
        "dbp":  live_dbp,
        "sleep_interruptions": current_readings.get("sleep_interruptions", 0),
        "light_wake_count":    current_readings.get("light_wake_count", 0),
    }
    return result


def run_trend_agent(sensor_history: list, patient_profile: dict, client) -> dict:
    trends = compute_trends(sensor_history)
    condition = patient_profile.get("primary_condition", "general")

    history_table = "\n".join(
        f"  Day {d['day']} ({d['date']}): HR={d['hr_resting_bpm']}bpm  SpO2={d['spo2_pct']}%  "
        f"Temp={d['temp_c']}°C  Weight={d['weight_lbs']}lbs  Steps={d['steps']}  Wakes={d['sleep_interruptions']}"
        for d in sensor_history
    )

    system = (
        "You are a longitudinal health trend analyst for post-discharge monitoring. "
        "Expertise: detecting slow, subtle deterioration that acute monitoring misses. "
        "Slow drift across multiple parameters is more ominous than a single spike. "
        "Tailor interpretation to the patient's condition. Return valid JSON only."
    )
    user = f"""Analyze 7-day post-discharge trends for {patient_profile['name']}, {patient_profile['age']}yo.
Diagnosis: {patient_profile['diagnosis']} | Condition context: {condition}

7-DAY HISTORY:
{history_table}

PRE-COMPUTED STATISTICAL TRENDS:
  HR slope:          +{trends['hr_slope_bpm_per_day']} bpm/day  ({trends['hr_range']})
  SpO2 slope:        {trends['spo2_slope_pct_per_day']}%/day  ({trends['spo2_range']})
  Temperature slope: +{trends['temp_slope_per_day']}°C/day  ({trends['temp_range']})
  Weight change:     {'+' if trends['weight_change_lbs_7days'] >= 0 else ''}{trends['weight_change_lbs_7days']} lbs total ({trends['weight_slope_lbs_per_day']} lbs/day)
  Activity:          peak {trends['steps_peak']} → current {trends['steps_current']} steps (−{trends['steps_decline_pct']}% from peak)
  Nocturnal wakes:   {trends['sleep_wakes_range']} (slope +{trends['sleep_wakes_slope_per_day']}/night)
  RR slope:          +{trends['rr_slope_per_day']} breaths/min/day

CONDITION-SPECIFIC THRESHOLDS:
  CHF: Weight >5 lbs/week = AHA red flag | COPD: SpO2 decline >3% from baseline = urgent
  SSI: Temp trending >38°C after Day 3 = concern | Activity decline >30% = 2.8× readmission risk

Return JSON ONLY:
{{
  "trend_interpretations": {{
    "vital_trajectory": "overall vital sign direction",
    "weight_or_temp": "weight gain (CHF) or temp trend (post-surgical) interpretation",
    "activity": "activity decline interpretation with risk multiplier if applicable",
    "sleep": "sleep disruption interpretation"
  }},
  "concerning_trends": ["specific trend with numeric evidence"],
  "trend_convergence": "are multiple independent trends moving in the same deteriorating direction?",
  "summary": "3-4 sentence narrative of the 7-day picture",
  "severity": "low|medium|high|critical"
}}"""

    result = _parse_json(_call(client, system, user, max_tokens=2000))
    result["_trends"] = trends
    return result


def run_self_report_agent(daily_reports: list, client) -> dict:
    reports_text = "\n".join(f'  Day {r["day"]}: "{r["text"]}"' for r in daily_reports)

    system = (
        "You are a clinical NLP agent specializing in patient-reported outcomes. "
        "Extract structured clinical signals from informal patient language. "
        "Key skill: patients minimize symptoms — recognize clinical significance behind informal language. "
        "Return valid JSON only."
    )
    user = f"""Analyze 7 days of patient check-ins. Extract clinical signal from informal language.

PATIENT CHECK-IN HISTORY:
{reports_text}

Look for: symptom trajectory, functional status changes, clinical red flags,
minimization language masking severity, directional change Day 1 → Day 7.

Return JSON ONLY:
{{
  "symptom_trajectory": "improving|stable|gradually_worsening|acutely_worsening",
  "symptoms_identified": {{
    "primary_concern": "main symptom and its Day 1→7 progression",
    "secondary_concerns": "other symptoms and their evolution",
    "functional_decline": "how functional capacity has changed"
  }},
  "red_flag_phrases": ["direct quotes signaling clinical concern — include day number"],
  "minimization_language": ["patient downplaying symptoms"],
  "patient_concern_level": "low|moderate|high",
  "summary": "3-4 sentence NLP analysis of trajectory",
  "severity": "low|medium|high|critical"
}}"""

    return _parse_json(_call(client, system, user, max_tokens=2000))


def run_knowledge_agent(signal_out: dict, trend_out: dict, self_report_out: dict,
                        client, web_research: dict = None, patient_profile: dict = None) -> dict:
    news2 = signal_out.get("_news2", {})
    trends = trend_out.get("_trends", {})
    meds = patient_profile.get("medications", []) if patient_profile else []

    state = f"""
SIGNAL: {signal_out.get('summary', 'N/A')} [Severity: {signal_out.get('severity', '?')}]
TREND: {trend_out.get('summary', 'N/A')} [Severity: {trend_out.get('severity', '?')}]
SELF-REPORT: {self_report_out.get('summary', 'N/A')} [Severity: {self_report_out.get('severity', '?')}]

KEY METRICS:
  SpO2: {news2.get('components', {}).get('spo2', {}).get('value', '?')}%
  HR: {news2.get('components', {}).get('heart_rate', {}).get('value', '?')} bpm
  Temp today: {news2.get('components', {}).get('temperature', {}).get('value', '?')}°C
  Temp slope: +{trends.get('temp_slope_per_day', '?')}°C/day
  Weight change: {trends.get('weight_change_lbs_7days', '?')} lbs
  Activity decline: {trends.get('steps_decline_pct', '?')}%
  Red flag phrases: {self_report_out.get('red_flag_phrases', [])}

CURRENT MEDICATIONS: {', '.join(meds) if meds else 'None listed'}
"""

    # Inject live web research if available
    lit_context = ""
    if web_research and not web_research.get("error"):
        from web_research import format_for_agents
        lit_context = "\n\n" + format_for_agents(web_research)

    system = (
        "You are a medical knowledge retrieval agent (RAG). "
        "Apply the provided clinical knowledge base AND any literature research context to the patient state. "
        "Always cite specific criteria by name and threshold. When literature context is present, "
        "cross-reference it with the built-in guidelines and flag any additional risks it reveals. "
        "IMPORTANT: Always consider the patient's current medications — check for relevant drug effects, "
        "drug-condition interactions (e.g. diuretics in CHF, beta-blockers masking tachycardia), "
        "and whether medication adherence issues may explain any reported symptoms. "
        "Return valid JSON only."
    )
    user = f"""Apply clinical guidelines to this post-discharge patient state.

CURRENT STATE:
{state}

CLINICAL KNOWLEDGE BASE:
{json.dumps(CLINICAL_KNOWLEDGE_BASE, indent=2)}{lit_context}

Identify which specific criteria are triggered. Be precise — state the threshold and whether it is met.
If literature research context is provided above, incorporate its complication thresholds and red flags.
For medications: note any drug-condition interactions, expected medication effects on vitals, and
whether current readings could reflect medication side effects vs true deterioration.

Return JSON ONLY:
{{
  "criteria_applied": [
    {{
      "guideline": "name",
      "criteria_triggered": ["criterion MET — include threshold value"],
      "criteria_not_triggered": ["criterion NOT YET met — how close?"],
      "clinical_significance": "why this matters"
    }}
  ],
  "medication_considerations": [
    {{
      "medication": "drug name",
      "relevant_effect": "how this drug affects vital sign interpretation or risk",
      "interaction_flag": "any drug-condition concern"
    }}
  ],
  "red_flags_triggered": ["specific red flag WITH supporting data"],
  "red_flags_approaching": ["not yet met but trending toward"],
  "most_applicable_guideline": "which guideline is most relevant and why",
  "summary": "3-4 sentence evidence-based assessment citing specific guidelines and noting medication context",
  "severity": "low|medium|high|critical"
}}"""

    return _parse_json(_call(client, system, user, max_tokens=4000))


def run_skeptic_agent(signal_out: dict, trend_out: dict, self_report_out: dict,
                      knowledge_out: dict, patient_profile: dict, client) -> dict:
    concerns = f"""
SIGNAL ALERTS: {signal_out.get('acute_alerts', [])}
BORDERLINE: {signal_out.get('borderline_concerns', [])}
NEWS2: {signal_out.get('news2_score', '?')}/15 ({signal_out.get('news2_risk_level', '?')} risk)
VITAL SUMMARY: {signal_out.get('summary', '')}
TRENDS: {trend_out.get('concerning_trends', [])}
TREND SUMMARY: {trend_out.get('summary', '')}
SELF-REPORT QUOTES: {self_report_out.get('red_flag_phrases', [])}
SELF-REPORT TRAJECTORY: {self_report_out.get('symptom_trajectory', '?')}
GUIDELINES TRIGGERED: {knowledge_out.get('red_flags_triggered', [])}
GUIDELINES APPROACHING: {knowledge_out.get('red_flags_approaching', [])}
PATIENT MEDS: {', '.join(patient_profile['medications'])}
CONDITION: {patient_profile.get('primary_condition', 'general')}
AGE/SEX: {patient_profile.get('age', '?')}yo {patient_profile.get('sex', '?')}
"""

    system = (
        "You are the Adversarial Skeptic Agent in a multi-agent clinical AI debate system. "
        "Your job: RIGOROUSLY argue against premature escalation to prevent alarm fatigue. "
        "For each concerning finding, you MUST run a structured internal debate: "
        "state the strongest possible benign hypothesis, then honestly and specifically dismantle it. "
        "Do not be lazy — a weak hypothesis followed by a weak rebuttal is useless. "
        "Force yourself to find the most medically plausible benign explanation (medication effect, "
        "post-discharge recovery curve, diurnal variation, measurement artifact, anxiety amplification, "
        "deconditioning, sleep disruption, dietary salt, environment). Then tear it apart using "
        "the actual data. Be specific with numbers. "
        "Your output feeds the final reconciler — it needs substance to weigh against the clinical signals. "
        "Return valid JSON only."
    )
    user = f"""Run a rigorous structured debate on each concerning clinical finding.
For every finding: argue it away, then rebut your own argument with the actual evidence.

FINDINGS TO DEBATE:
{concerns}

DEBATE RULES:
- Hypothesis must be medically specific (cite mechanism, drug, or physiology — not vague)
- Rebuttal must cite specific numbers/data from the findings above
- Be intellectually honest: rate how well your hypothesis actually holds up
- Cover at least 3-4 distinct findings

Return JSON ONLY:
{{
  "debate": [
    {{
      "finding": "specific concern being debated (e.g. 'SpO2 91% — NEWS2 red flag')",
      "hypothesis": "most compelling benign explanation — 2-3 sentences, cite mechanism or drug effect",
      "evidence_for_hypothesis": "what in the data supports this benign view",
      "rebuttal": "why this hypothesis fails — 2-3 sentences, cite specific numbers that contradict it",
      "verdict": "hypothesis_holds|hypothesis_weakened|hypothesis_rejected",
      "hypothesis_strength": "strong|moderate|weak"
    }}
  ],
  "strongest_hypothesis": "the single best reason across all findings not to escalate — 1-2 sentences",
  "strongest_hypothesis_debunked": "why even this best case ultimately fails given the full picture — 1-2 sentences",
  "overall_benign_narrative": "most coherent non-alarming explanation for everything together — 2-3 sentences",
  "overall_verdict": "escalation_warranted|watchful_wait|no_action",
  "skeptic_confidence": "low|moderate|high",
  "where_skepticism_fails": "which findings genuinely cannot be explained away and precisely why — be specific"
}}"""

    result = _parse_json(_call(client, system, user, max_tokens=5000))
    if not result.get("strongest_hypothesis"):
        result["strongest_hypothesis"] = result.pop("strongest_benign_case", "No compelling benign case — findings converge on clinical deterioration.")
    if not result.get("debate") and result.get("counterarguments"):
        # Migrate old format
        result["debate"] = [
            {
                "finding": c.get("finding", ""),
                "hypothesis": c.get("benign_explanation", ""),
                "evidence_for_hypothesis": c.get("what_would_confirm_benign", ""),
                "rebuttal": "See where_skepticism_fails.",
                "verdict": "hypothesis_weakened",
                "hypothesis_strength": c.get("argument_strength", "weak"),
            }
            for c in result["counterarguments"]
        ]
    if not result.get("where_skepticism_fails"):
        result["where_skepticism_fails"] = "Convergent signals from multiple sources reduce likelihood of benign explanation."
    if not result.get("overall_benign_narrative"):
        result["overall_benign_narrative"] = "N/A — multi-source signal convergence."
    return result


def run_reconciler_agent(signal_out: dict, trend_out: dict, self_report_out: dict,
                         knowledge_out: dict, skeptic_out: dict, patient_profile: dict,
                         days_post_discharge: int, client) -> dict:
    trends = trend_out.get("_trends", {})
    news2 = signal_out.get("_news2", {})

    evidence_for = f"""
FOR: Signal={signal_out.get('severity','?').upper()} | Trend={trend_out.get('severity','?').upper()} | NLP={self_report_out.get('severity','?').upper()} | Knowledge={knowledge_out.get('severity','?').upper()}
Summaries: {signal_out.get('summary','')} / {trend_out.get('summary','')} / {self_report_out.get('summary','')}
Red flags triggered: {knowledge_out.get('red_flags_triggered', [])}
Key metrics: SpO2={news2.get('components',{}).get('spo2',{}).get('value','?')}% | Temp={news2.get('components',{}).get('temperature',{}).get('value','?')}°C
Weight Δ={trends.get('weight_change_lbs_7days','?')} lbs | Activity −{trends.get('steps_decline_pct','?')}%
Patient red flags: {self_report_out.get('red_flag_phrases', [])}
Trend convergence: {trend_out.get('trend_convergence', 'N/A')}
"""
    # Build full debate transcript so the Reconciler can respond round-by-round
    debate_rounds = skeptic_out.get("debate") or []
    debate_transcript = "\n".join(
        f"  Round {i+1} [{r.get('finding','?')}]\n"
        f"    Hypothesis ({r.get('hypothesis_strength','?')}): {r.get('hypothesis','')}\n"
        f"    Evidence for: {r.get('evidence_for_hypothesis','')}\n"
        f"    Skeptic self-rebuttal: {r.get('rebuttal','')}\n"
        f"    Skeptic verdict: {r.get('verdict','?')}"
        for i, r in enumerate(debate_rounds)
    ) or "  No debate rounds available."
    evidence_against = f"""
AGAINST (Skeptic Agent full debate):
Strongest benign case: {skeptic_out.get('strongest_hypothesis', skeptic_out.get('strongest_benign_case', ''))}
Skeptic overall verdict: {skeptic_out.get('overall_verdict', 'N/A')}
Skeptic confidence: {skeptic_out.get('skeptic_confidence', 'N/A')}
Where skepticism fails (per Skeptic): {skeptic_out.get('where_skepticism_fails', '')}

DEBATE TRANSCRIPT:
{debate_transcript}
"""

    from patient_data import CLINICAL_KNOWLEDGE_BASE
    escalation_kb = CLINICAL_KNOWLEDGE_BASE.get("escalation_protocol", {})
    condition = patient_profile.get("primary_condition", "general")
    condition_overrides = escalation_kb.get("condition_overrides", {}).get(condition, [])

    # Count data quality for upstream agents to inform confidence level
    upstream_parse_errors = sum(1 for o in [signal_out, trend_out, self_report_out, knowledge_out, skeptic_out]
                                if o.get("_parse_error"))
    data_days = len([r for r in patient_profile.get("sensor_history_ref", []) if r]) if "sensor_history_ref" in patient_profile else 7

    system = (
        "You are the Reconciler/Judge Agent — final arbiter of clinical escalation. "
        "Weigh ALL evidence from 5 independent data streams against the Skeptic's best arguments. "
        "Key principle: convergent evidence from MULTIPLE INDEPENDENT streams cannot all be coincidental "
        "benign explanations simultaneously. Show your work explicitly.\n\n"
        "ESCALATION PROTOCOL (static clinical memory — apply to every assessment):\n"
        "LEVEL 0 (Routine): NEWS2 0-2, stable trends → standard monitoring, scheduled follow-up only.\n"
        "LEVEL 1 (Enhanced): NEWS2 3-4 OR single mild trend → it would be appropriate for patient to "
        "contact care team within 24-48 hours.\n"
        "LEVEL 2 (Urgent): NEWS2 5-6 OR multiple converging trends, CHF weight >5 lbs/week, "
        "COPD rescue inhaler escalation → it would be appropriate to notify attending physician within 2-4 hours.\n"
        "LEVEL 3 (Emergency): NEWS2 7+, SpO2 <88%, HR >130 or <40, systolic <90, acute chest pain, "
        "severe dyspnea → it would be appropriate to activate emergency medical services (911) immediately.\n"
        f"CONDITION-SPECIFIC OVERRIDES for {condition.upper()}: {'; '.join(condition_overrides)}\n\n"
        "CONFIDENCE DEFINITION — score your own certainty:\n"
        "  high   = all 5 upstream agents returned complete data, signals are internally consistent, "
        "clear convergence across independent streams, no data gaps in sensor history.\n"
        "  medium = 1-2 upstream agents had parse issues OR mild contradictions between agent outputs "
        "OR ≤5 days of sensor history OR signals only partially converge.\n"
        "  low    = 3+ upstream agents failed/had parse errors OR major contradictions OR <4 days data "
        "OR signals completely diverge — assessment is a best-effort estimate under uncertainty.\n\n"
        "IMPORTANT: The escalation_recommendation field must state what action 'would be appropriate' — "
        "this system does NOT take action itself. All clinical decisions remain with the care team.\n"
        "Return valid JSON only. Every field is required — never omit risk_score or risk_level."
    )
    upstream_quality = f"Upstream agent errors: {upstream_parse_errors}/5 | Data days available: {days_post_discharge}"
    round_labels = "\n".join(
        f"  Round {i+1}: \"{r.get('finding','?')}\""
        for i, r in enumerate(debate_rounds)
    ) or "  (no rounds)"
    user = f"""Reconcile all evidence for {patient_profile['name']}, Day {days_post_discharge} post-{patient_profile['diagnosis']}.

{evidence_for}
{evidence_against}

DATA QUALITY: {upstream_quality}

Your task has two parts:
1. For EACH debate round above, rule on the Skeptic's hypothesis — ACCEPT it (it lowers your score) or OVERRULE it (it does not). Be specific: cite the data that makes you accept or reject.
2. Produce the final calibrated risk assessment.

Debate rounds to rule on:
{round_labels}

Return JSON ONLY (all fields required):
{{
  "debate_rebuttals": [
    {{
      "round": <1-based integer matching the Skeptic's round>,
      "finding": "the contested finding",
      "skeptic_hypothesis": "Skeptic's benign explanation in one sentence",
      "ruling": "accepted|overruled",
      "reasoning": "1-2 sentences: specific data that drives your ruling, cite numbers"
    }}
  ],
  "risk_score": <integer 1-100, never 0 unless truly no risk signal at all>,
  "risk_level": "low|moderate|high|critical",
  "primary_drivers": ["specific driver with supporting numbers", "second driver"],
  "mitigating_factors": ["valid skeptic point that actually reduced the score"],
  "convergence_argument": "why simultaneous findings across 5 independent streams cannot all be coincidental",
  "skeptic_rebuttal": "direct response to the Skeptic's overall_benign_narrative",
  "confidence": "low|medium|high (per the definitions above)",
  "recommended_action": "specific clinical action in 1-2 sentences",
  "time_sensitivity": "immediately|within_4h|within_24h|within_48h|routine",
  "escalation_level": <integer 0-3 matching the escalation protocol levels above>,
  "escalation_recommendation": "Full text starting with 'it would be appropriate to...' — do NOT claim the AI takes action",
  "rationale": "4-6 sentence reasoning chain: data quality → signal convergence → score justification → confidence rationale"
}}"""

    result = _parse_json(_call(client, system, user, max_tokens=4000))

    # Safety net: if parse failed or critical fields are missing, flag it clearly
    if result.get("_parse_error") or result.get("risk_score") is None:
        result["confidence"] = "low"
        result["_assessment_note"] = "Parse error or missing fields — re-run analysis for accurate results"
        if "risk_score" not in result or result.get("risk_score") is None:
            result["risk_score"] = 0
        if "risk_level" not in result:
            result["risk_level"] = "unknown"

    return result


def run_brief_agent(reconciler_out: dict, patient_profile: dict, days_post_discharge: int,
                    client, web_research: dict = None) -> str:
    # Build optional research context block for the brief
    lit_section = ""
    if web_research and not web_research.get("error"):
        flags = (web_research.get("red_flags") or [])[:5]
        highlights = (web_research.get("evidence_highlights") or [])[:2]
        sources = [s.get("name","") for s in (web_research.get("sources_fetched") or [])]
        lit_section = f"""
LITERATURE-BASED CONTEXT (incorporated from {', '.join(sources)}):
{web_research.get('diagnosis_context', '')}

Evidence-based red flags for this diagnosis:
{chr(10).join(f'  • {f}' for f in flags)}

Key findings:
{chr(10).join(f'  [{e.get("source","?")}]: {e.get("key_finding","")}' for e in highlights)}

Patient-specific risk narrative:
{web_research.get('tailored_risk_narrative', '')[:500]}
"""

    assessment_note = reconciler_out.get("_assessment_note", "")
    note_block = f"\n⚠ ASSESSMENT NOTE: {assessment_note}" if assessment_note else ""

    system = (
        "You are a Clinical Brief Agent. Convert AI risk assessments into SBAR format — "
        "the standard healthcare handoff format. Write for a care coordinator who needs to act. "
        "Be specific: include numbers, dates, exact action items with timeframes. "
        "When literature research context is provided, weave it into the brief to make "
        "recommendations evidence-based and specific to this patient's diagnosis.\n\n"
        "FORMATTING RULES — follow exactly:\n"
        "1. Each section header is on its own line: **SITUATION**, **BACKGROUND**, **ASSESSMENT**, **RECOMMENDATION**\n"
        "2. Section content immediately follows the header with one blank line between them\n"
        "3. RECOMMENDATION items use this EXACT format — number, period, space, timeframe colon, action — ALL ON ONE LINE:\n"
        "   1. Within 24 hours: [specific action referencing patient name and exact values]\n"
        "   2. Daily: [monitoring action with threshold]\n"
        "   3. Within 48 hours: [follow-up action]\n"
        "   4. If [condition]: [contingency action]\n"
        "   5. At next visit: [assessment action]\n"
        "4. Do NOT put the number on one line and the content on the next line\n"
        "5. Do NOT put the timeframe on one line and the action on the next line\n"
        "6. The footnote line goes after the --- separator"
    )
    user = f"""Create an SBAR clinical brief.

PATIENT:
  {patient_profile['name']} | {patient_profile['id']} | {patient_profile['age']}yo {patient_profile['sex']}
  Dx: {patient_profile['diagnosis']}
  Discharged: {patient_profile['discharge_date']} (Day {days_post_discharge} today)
  Meds: {', '.join(patient_profile['medications'])}
  Baseline risk: {patient_profile['30day_readmission_risk']}{note_block}

AI RISK ASSESSMENT:
  Score: {reconciler_out.get('risk_score', '?')}/100 — {str(reconciler_out.get('risk_level', '?')).upper()}
  Confidence: {reconciler_out.get('confidence', '?')}
  Primary drivers: {reconciler_out.get('primary_drivers', [])}
  Mitigating factors: {reconciler_out.get('mitigating_factors', [])}
  Action: {reconciler_out.get('recommended_action', '?')}
  Time sensitivity: {str(reconciler_out.get('time_sensitivity', '?')).replace('_', ' ').upper()}
  Rationale: {reconciler_out.get('rationale', '?')}
{lit_section}
Write the SBAR using EXACTLY this structure. RECOMMENDATION items must be on a single line each:

**SITUATION**
[1-2 sentences: who, what concern, why reaching out now]

**BACKGROUND**
[2-3 sentences: relevant clinical context, key worsening metrics with numbers, reference literature findings where relevant]

**ASSESSMENT**
[3-4 sentences: AI assessment with specific data points, risk score, top drivers, cite any literature-based thresholds that are triggered]

**RECOMMENDATION**
1. Within [timeframe]: [action specific to this patient with exact values]
2. [Timeframe]: [second action with threshold]
3. [Timeframe]: [third action]
4. If [condition occurs]: [contingency action]
5. At [timeframe]: [follow-up assessment]

---
*Nightingale | {patient_profile['id']} | 2026-06-20 | {MODEL}*"""

    with client.messages.stream(
        model=MODEL,
        max_tokens=3000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        return _extract_text(stream.get_final_message())


# ─── Brief extraction ─────────────────────────────────────────────────────────

def extract_brief(agent_id: str, result) -> dict:
    """Pull display-critical fields out of each agent's full output."""
    if agent_id == "signal":
        v = result.get("_vitals", {})
        return {
            "news2_score": result.get("news2_score"),
            "news2_risk": result.get("news2_risk_level"),
            "severity": result.get("severity", "unknown"),
            "summary": result.get("summary", ""),
            "alerts": (result.get("acute_alerts") or [])[:3],
            "concerns": (result.get("borderline_concerns") or [])[:3],
            "hr":  v.get("hr"),
            "rr":  v.get("rr"),
            "sbp": v.get("sbp"),
            "dbp": v.get("dbp"),
            "sleep_interruptions": v.get("sleep_interruptions", 0),
            "light_wake_count":    v.get("light_wake_count", 0),
        }
    if agent_id == "trend":
        t = result.get("_trends", {})
        return {
            "severity": result.get("severity", "unknown"),
            "summary": result.get("summary", ""),
            "concerning_trends": (result.get("concerning_trends") or [])[:3],
            "hr_slope": t.get("hr_slope_bpm_per_day"),
            "weight_change": t.get("weight_change_lbs_7days"),
            "steps_decline": t.get("steps_decline_pct"),
            "temp_slope": t.get("temp_slope_per_day"),
        }
    if agent_id == "self_report":
        return {
            "severity": result.get("severity", "unknown"),
            "summary": result.get("summary", ""),
            "trajectory": result.get("symptom_trajectory", "unknown"),
            "red_flags": (result.get("red_flag_phrases") or [])[:3],
        }
    if agent_id == "knowledge":
        raw_summary = result.get("summary", "")
        # If _parse_json fell back to raw text, "summary" is JSON — strip it
        safe_summary = "" if (result.get("_parse_error") or str(raw_summary).lstrip().startswith(("{", "["))) else raw_summary
        return {
            "severity": result.get("severity", "unknown"),
            "summary": safe_summary,
            "triggered": (result.get("red_flags_triggered") or [])[:3],
            "approaching": (result.get("red_flags_approaching") or [])[:2],
            "most_applicable": result.get("most_applicable_guideline", ""),
            "parse_error": bool(result.get("_parse_error")),
        }
    if agent_id == "skeptic":
        return {
            "strongest_case": result.get("strongest_hypothesis", result.get("strongest_benign_case", "")),
            "strongest_case_debunked": result.get("strongest_hypothesis_debunked", ""),
            "where_fails": result.get("where_skepticism_fails", ""),
            "overall_verdict": result.get("overall_verdict", ""),
            "confidence": result.get("skeptic_confidence", "unknown"),
            "debate": (result.get("debate") or [])[:5],
        }
    if agent_id == "reconciler":
        return {
            "risk_score": result.get("risk_score", 0),
            "risk_level": result.get("risk_level", "unknown"),
            "confidence": result.get("confidence", "unknown"),
            "primary_drivers": (result.get("primary_drivers") or [])[:2],
            "recommended_action": result.get("recommended_action", ""),
            "time_sensitivity": result.get("time_sensitivity", ""),
            "rationale": result.get("rationale", ""),
            "skeptic_rebuttal": result.get("skeptic_rebuttal", ""),
            "convergence_argument": result.get("convergence_argument", ""),
            "debate_rebuttals": (result.get("debate_rebuttals") or [])[:5],
            "escalation_level": result.get("escalation_level", 0),
            "escalation_recommendation": result.get("escalation_recommendation", ""),
        }
    if agent_id == "brief":
        return {"text": result if isinstance(result, str) else ""}
    return {}


# ─── Full pipeline orchestrator ───────────────────────────────────────────────

_STAGE_LABELS = {
    "signal":      "Signal Agent",
    "trend":       "Trend Agent",
    "self_report": "Self-Report NLP",
    "knowledge":   "Medical Knowledge (RAG)",
    "skeptic":     "Adversarial Skeptic",
    "reconciler":  "Reconciler / Judge",
    "brief":       "Clinical Brief (SBAR)",
    "escalation":  "Escalation (Fetch)",
}


def run_full_pipeline(
    patient_data: dict,
    client,
    callbacks: dict = None,
    log_dir: str = "logs",
    *,
    use_mesh: bool = True,
    web_research: dict = None,
) -> tuple:
    """Run the full 8-agent pipeline.

    Primary path — Fetch.ai mesh (use_mesh=True, default):
      All 8 agents are real uAgents in a Bureau. The Coordinator handles
      governance (escalation gate, audit trail) — Band is not used.
      Arize tracing wraps the whole pipeline span.

    Fallback path — direct calls (use_mesh=False or uagents unavailable):
      ThreadPoolExecutor fan-out for the parallel trio, then Band-governed
      sequential stages. Degrades gracefully if any dep is missing.

    Returns: (full_log dict, log_file_path string)
    """
    on_start    = (callbacks or {}).get("on_start",    lambda *_: None)
    on_complete = (callbacks or {}).get("on_complete", lambda *_: None)

    profile    = patient_data["profile"]
    history    = patient_data["sensor_history"]
    days       = len(history)
    patient_id = profile.get("id", "unknown")

    tracing.init_tracing()

    full_log = {
        "patient_id":         patient_id,
        "patient_name":       profile.get("name", "Unknown"),
        "analysis_timestamp": datetime.datetime.now().isoformat(),
        "model":              MODEL,
        "mesh":               False,
        "agents":             {},
    }

    with tracing.span("pipeline", patient_id=patient_id, mesh=bool(use_mesh)):
        trace_id = tracing.current_trace_id()

        # ── Fetch.ai path: every stage is a real uAgent ───────────────────────
        if use_mesh:
            from mesh import fetch_mesh as fm
            if not fm.is_available():
                full_log["mesh_error"] = "uagents not installed — falling back to direct calls"
                use_mesh = False
            else:
                try:
                    mesh_result = fm.run_full_pipeline_via_mesh(
                        patient_data, client,
                        callbacks={"on_start": on_start, "on_complete": on_complete},
                        web_research=web_research,
                    )
                    # Merge agent records produced by the coordinator
                    full_log["agents"].update(mesh_result["full_log"]["agents"])
                    full_log["mesh"]           = True
                    full_log["mesh_addresses"] = mesh_result["mesh_addresses"]

                    results        = mesh_result["results"]
                    reconciler_out = results.get("reconciler", {})
                    sbar_text      = results.get("brief", "")
                    assessment     = mesh_result["assessment"]
                    gate           = mesh_result["gate"]
                    audit          = mesh_result["audit"]
                except Exception as exc:
                    full_log["mesh_error"] = str(exc)
                    use_mesh = False   # fall through to direct path

        # ── Fallback: ThreadPoolExecutor + Band ───────────────────────────────
        if not use_mesh:
            current = history[-1]
            reports = patient_data["self_reports"]

            def record(agent_id, label, result, elapsed):
                brief = extract_brief(agent_id, result)
                full_log["agents"][agent_id] = {
                    "label":           label,
                    "elapsed_seconds": elapsed,
                    "brief":           brief,
                    "full_output":     result if not isinstance(result, str) else {"text": result},
                }
                return brief

            def step(agent_id, label, fn, *args):
                on_start(agent_id, label)
                t0 = time.time()
                with tracing.span(f"agent.{agent_id}"):
                    result = fn(*args)
                elapsed = round(time.time() - t0, 1)
                brief = record(agent_id, label, result, elapsed)
                on_complete(agent_id, label, brief, elapsed)
                return result

            on_start("signal",      _STAGE_LABELS["signal"])
            on_start("trend",       _STAGE_LABELS["trend"])
            on_start("self_report", _STAGE_LABELS["self_report"])
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                f_sig = pool.submit(run_signal_agent,      current, profile, client)
                f_trn = pool.submit(run_trend_agent,       history, profile, client)
                f_nlp = pool.submit(run_self_report_agent, reports, client)
                t0    = time.time()
                signal_out, trend_out, self_report_out = f_sig.result(), f_trn.result(), f_nlp.result()
            elapsed_p = round(time.time() - t0, 1)
            for sid, res in (("signal", signal_out), ("trend", trend_out), ("self_report", self_report_out)):
                brief = record(sid, _STAGE_LABELS[sid], res, elapsed_p)
                on_complete(sid, _STAGE_LABELS[sid], brief, elapsed_p)

            knowledge_out = step("knowledge", _STAGE_LABELS["knowledge"],
                                 run_knowledge_agent,
                                 signal_out, trend_out, self_report_out, client, web_research, profile)

            band = get_band_room(audit=AuditLog(echo=False))

            def skeptic_call():
                return step("skeptic", _STAGE_LABELS["skeptic"], run_skeptic_agent,
                            signal_out, trend_out, self_report_out, knowledge_out, profile, client)

            def reconciler_call(sk_out):
                return step("reconciler", _STAGE_LABELS["reconciler"], run_reconciler_agent,
                            signal_out, trend_out, self_report_out, knowledge_out, sk_out,
                            profile, days, client)

            with tracing.span("band.deliberate"):
                delib = band.deliberate(
                    patient_id=patient_id,
                    analysis_summary={"severities": {
                        k: full_log["agents"].get(k, {}).get("brief", {}).get("severity")
                        for k in ("signal", "trend", "self_report", "knowledge")
                    }},
                    skeptic_call=skeptic_call,
                    reconciler_call=reconciler_call,
                    trace_id=trace_id,
                )
            reconciler_out = delib.reconciler_out
            assessment     = delib.assessment
            with tracing.span("band.escalation_gate"):
                gate = band.escalation_gate(assessment)
            audit = band.audit

            sbar_text = step("brief", _STAGE_LABELS["brief"],
                             run_brief_agent, reconciler_out, profile, days, client, web_research)
            _run_escalation_step(full_log, audit, gate, assessment, on_start, on_complete)

    # ── Assemble top-level log fields (same shape for both paths) ─────────────
    full_log["risk_score"]           = reconciler_out.get("risk_score", 0)
    full_log["risk_level"]           = reconciler_out.get("risk_level", "unknown")
    full_log["sbar"]                 = sbar_text if isinstance(sbar_text, str) else sbar_text.get("text", "")
    full_log["recommended_action"]   = reconciler_out.get("recommended_action", "")
    full_log["time_sensitivity"]     = reconciler_out.get("time_sensitivity", "")
    full_log["escalation_level"]     = reconciler_out.get("escalation_level", 0)
    full_log["escalation_recommendation"] = reconciler_out.get("escalation_recommendation", "")
    full_log["web_research_incorporated"] = bool(web_research and not web_research.get("error"))
    full_log["web_research_sources"] = [
        s.get("name") for s in (web_research or {}).get("sources_fetched", [])
    ] if web_research else []

    if assessment:
        full_log["risk_score_normalized"] = round(assessment.risk_score, 3)
        full_log["recommend_escalation"]  = assessment.recommend_escalation
        full_log["self_correction"]       = _run_correction_loop(profile, assessment)
    full_log["gate_decision"]   = gate.model_dump() if gate else {}
    full_log["risk_threshold"]  = config.get_risk_threshold()
    full_log["trace_id"]        = trace_id
    full_log["tracing_enabled"] = tracing.is_enabled()
    full_log["band_audit"]      = audit.as_dicts() if audit else []

    os.makedirs(log_dir, exist_ok=True)
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"{patient_id}_{ts}.json")
    with open(log_file, "w") as f:
        json.dump(full_log, f, indent=2, default=str)

    return full_log, log_file


def _run_escalation_step(full_log, audit, gate, assessment, on_start, on_complete):
    """Governed output action: the Escalation uAgent (Fetch) fires only on a
    gated, authorized escalation; otherwise the decision is held + audited."""
    label = "Escalation (Band gate + Fetch)"
    on_start("escalation", label)
    t0 = time.time()

    uagent_address = None
    try:
        from mesh import fetch_mesh

        if fetch_mesh.is_available():
            uagent_address = fetch_mesh.escalation_uagent().address
    except Exception:
        uagent_address = None

    if gate.escalate:
        audit.append(
            "escalation_agent", "handoff_delivered",
            authority_granted=gate.authority_granted,
            payload={"via": "fetch.uagent", "address": uagent_address,
                     "risk_score": assessment.risk_score_100},
        )
    else:
        audit.append("escalation_agent", "escalation_held", payload={"reason": gate.reason})

    elapsed = round(time.time() - t0, 1)
    brief = {
        "escalated": gate.escalate,
        "authority_granted": gate.authority_granted,
        "reason": gate.reason,
        "uagent_address": uagent_address,
        "recommended_action": assessment.recommended_action,
        "time_sensitivity": assessment.time_sensitivity,
    }
    full_log["agents"]["escalation"] = {
        "label": label, "elapsed_seconds": elapsed, "brief": brief, "full_output": brief
    }
    on_complete("escalation", label, brief, elapsed)


def _run_correction_loop(profile: dict, assessment) -> dict:
    """One pass of the Arize self-correction loop, if ground truth is known.

    The patient's ``ground_truth_escalate`` (bool) is the label; a false positive
    raises RISK_THRESHOLD, a false negative lowers it. This is the single knob the
    whole observability feedback loop moves.
    """
    truth = profile.get("ground_truth_escalate")
    if truth is None:
        return {"ran": False, "reason": "no ground-truth label for this patient"}
    before = config.get_risk_threshold()
    score = evaluator.score_decision(assessment, truth)
    after = evaluator.adjust_threshold(assessment, truth)
    return {
        "ran": True,
        "ground_truth_escalate": truth,
        "predicted_escalate": assessment.recommend_escalation,
        "correct": assessment.recommend_escalation == truth,
        "calibration_score": round(score, 3),
        "threshold_before": before,
        "threshold_after": after,
    }
