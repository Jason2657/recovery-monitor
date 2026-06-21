"""
Synthetic patient scenarios for PostCare AI Monitor.
Three distinct post-discharge cases spanning different risk profiles.
"""

# ─── Shared clinical knowledge base ──────────────────────────────────────────

CLINICAL_KNOWLEDGE_BASE = {
    "news2": {
        "title": "NEWS2 (National Early Warning Score 2)",
        "description": (
            "Standardized early-warning scoring system. "
            "Any single parameter score of 3 triggers Medium risk regardless of total."
        ),
        "parameter_scoring": {
            "respiratory_rate": {"<=8": 3, "9-11": 1, "12-20": 0, "21-24": 2, ">=25": 3},
            "spo2_no_o2": {"<=91": 3, "92-93": 2, "94-95": 1, ">=96": 0},
            "systolic_bp": {"<=90": 3, "91-100": 2, "101-110": 1, "111-219": 0, ">=220": 3},
            "heart_rate": {"<=40": 3, "41-50": 1, "51-90": 0, "91-110": 1, "111-130": 2, ">=131": 3},
            "temperature_c": {"<=35.0": 3, "35.1-36.0": 1, "36.1-38.0": 0, "38.1-39.0": 1, ">=39.1": 2},
        },
        "risk_thresholds": {
            "0": "Low — routine monitoring",
            "1-4": "Low-Medium — 4-6 hourly monitoring",
            "5-6": "Medium — urgent review within 1 hour",
            "7+": "High — emergency response",
            "any_single_3": "Medium — urgent review regardless of total",
        },
    },
    "chf_decompensation": {
        "title": "Heart Failure Decompensation Warning Signs",
        "source": "AHA/ACC Heart Failure Guidelines",
        "red_flags": [
            "Weight gain >2 lbs in 24 hours OR >5 lbs in 1 week (fluid retention / decompensation)",
            "New or worsening dyspnea at rest or minimal exertion (walking to kitchen)",
            "Lower extremity edema not resolving by mid-day",
            "Orthopnea — new need for more pillows when supine",
            "Paroxysmal nocturnal dyspnea — waking from sleep breathless",
            "Resting HR >90 bpm OR increase >10 bpm from stable baseline",
            "SpO2 <94% at rest on room air",
            "Nocturia >2 episodes/night (fluid redistribution when supine)",
            "Marked decrease in activity tolerance",
        ],
        "key_insight": (
            "CHF decompensation is insidious — patients accumulate 5-10 lbs of fluid BEFORE severe "
            "symptoms. Early phone intervention (diuretic adjustment) prevents ~40% of 30-day "
            "readmissions. Daily weight is the single best early warning sign."
        ),
        "nocturia_mechanism": (
            "Nocturia in CHF = fluid redistribution when supine. Dependent edema drains into "
            "circulation, increases preload, triggers renal diuresis. Worsening nocturia despite "
            "stable Furosemide = worsening fluid overload, NOT a medication side effect."
        ),
    },
    "surgical_site_infection": {
        "title": "Surgical Site Infection (SSI) Early Warning Signs",
        "source": "CDC SSI Surveillance Guidelines / NICE NG125",
        "red_flags": [
            "Fever >38.0°C developing or persisting beyond post-op Day 3",
            "Increasing warmth, redness, or swelling at incision site (not improving with time)",
            "Redness spreading beyond wound margins (cellulitis)",
            "Wound drainage increasing or becoming purulent",
            "Increasing pain at wound site (expected pain should be decreasing)",
            "Failure to progress in physical therapy due to wound-related pain",
        ],
        "fever_context": (
            "Low-grade fever Days 1-3 post-op is normal (surgical stress response). "
            "Fever emerging or worsening after Day 3 is abnormal and suggests SSI, DVT, or PE. "
            "Temperature trend is more clinically significant than a single reading."
        ),
        "ssi_risk_factors": ["Diabetes", "Obesity", "Immunosuppression", "Prolonged surgery", "Age >65"],
    },
    "copd_exacerbation": {
        "title": "COPD Exacerbation & Readmission Warning Signs",
        "source": "GOLD COPD Guidelines 2024",
        "red_flags": [
            "SpO2 declining >3% from post-discharge baseline",
            "Respiratory rate >20/min at rest and increasing",
            "Increased rescue inhaler use (>4 puffs/day of SABA suggests inadequate control)",
            "Dyspnea worsening to rest dyspnea or minimal-exertion dyspnea",
            "Heart rate >90 bpm (SIRS criterion, also cor pulmonale marker)",
            "Activity decline >50% from post-discharge best",
            "Cough increasing in frequency or changing character",
            "Sleeping propped up to breathe (orthopnea)",
        ],
        "key_insight": (
            "COPD readmission risk is highest in first 30 days. SpO2 <92% at rest represents "
            "respiratory failure threshold requiring urgent review. GOLD guidelines consider "
            "SpO2 <88% an indication for supplemental oxygen. Trends matter more than single values "
            "in COPD patients who have chronically lower baselines."
        ),
        "copd_spo2_note": (
            "COPD patients may have lower baseline SpO2 (90-95%) vs healthy individuals. "
            "The DECLINE in SpO2 from the patient's own baseline is more significant than "
            "comparison to population norms. A decline of >3% is clinically significant."
        ),
    },
    "activity_decline": {
        "title": "Activity Decline as Post-Discharge Readmission Predictor",
        "evidence": [
            "Step count decline >30% from post-discharge peak → 2.8× 30-day readmission risk (NEJM Evidence 2024)",
            "Maintaining >2,500 steps/day associated with significantly lower readmission rates",
            "Accelerating decline trajectory is MORE predictive than absolute step count",
            "Activity decline >50% from peak strongly suggests functional deterioration",
        ],
    },
    "sirs_criteria": {
        "title": "SIRS (Systemic Inflammatory Response Syndrome)",
        "criteria": [
            "Temperature >38.0°C or <36.0°C",
            "Heart rate >90 bpm",
            "Respiratory rate >20/min",
            "WBC >12,000 or <4,000/µL",
        ],
        "note": "2+ criteria = systemic inflammation. Can indicate infection, DVT, or other complications.",
    },
}

# ─── Patient 1: James Morrison — Post-ADHF, Day 7, HIGH risk ─────────────────
# Scenario: Insidious CHF decompensation. NEWS2=1 (Low) but 5 converging trends.

_james_profile = {
    "id": "PT-7421",
    "name": "James Morrison",
    "age": 72,
    "sex": "Male",
    "diagnosis": "Acute Decompensated Heart Failure (ADHF)",
    "primary_condition": "chf",
    "discharge_date": "2026-06-13",
    "medications": ["Furosemide 40mg QD", "Lisinopril 5mg QD", "Carvedilol 6.25mg BID", "Spironolactone 25mg QD"],
    "baseline_hr_bpm": 68,
    "baseline_spo2_pct": 97,
    "baseline_weight_lbs": 185.0,
    "risk_factors": ["Hypertension", "Type 2 Diabetes", "Prior MI (2019)"],
    "30day_readmission_risk": "High (HF-READMIT Score: 7/10)",
}

_james_history = [
    {"day": 1, "date": "2026-06-14", "hr_resting_bpm": 68, "hr_avg_bpm": 74, "spo2_pct": 97,
     "rr_breaths_per_min": 15, "temp_c": 36.7, "systolic_bp": 128, "diastolic_bp": 82,
     "weight_lbs": 185.0, "steps": 1823, "sleep_hours": 7.2, "sleep_interruptions": 1},
    {"day": 2, "date": "2026-06-15", "hr_resting_bpm": 69, "hr_avg_bpm": 75, "spo2_pct": 97,
     "rr_breaths_per_min": 15, "temp_c": 36.8, "systolic_bp": 126, "diastolic_bp": 80,
     "weight_lbs": 185.2, "steps": 2100, "sleep_hours": 7.0, "sleep_interruptions": 1},
    {"day": 3, "date": "2026-06-16", "hr_resting_bpm": 70, "hr_avg_bpm": 76, "spo2_pct": 97,
     "rr_breaths_per_min": 16, "temp_c": 36.6, "systolic_bp": 130, "diastolic_bp": 84,
     "weight_lbs": 185.8, "steps": 2450, "sleep_hours": 6.8, "sleep_interruptions": 2},
    {"day": 4, "date": "2026-06-17", "hr_resting_bpm": 72, "hr_avg_bpm": 79, "spo2_pct": 96,
     "rr_breaths_per_min": 16, "temp_c": 36.9, "systolic_bp": 134, "diastolic_bp": 86,
     "weight_lbs": 186.5, "steps": 2200, "sleep_hours": 6.2, "sleep_interruptions": 3},
    {"day": 5, "date": "2026-06-18", "hr_resting_bpm": 74, "hr_avg_bpm": 81, "spo2_pct": 95,
     "rr_breaths_per_min": 17, "temp_c": 37.0, "systolic_bp": 138, "diastolic_bp": 88,
     "weight_lbs": 187.5, "steps": 1850, "sleep_hours": 5.8, "sleep_interruptions": 4},
    {"day": 6, "date": "2026-06-19", "hr_resting_bpm": 76, "hr_avg_bpm": 83, "spo2_pct": 95,
     "rr_breaths_per_min": 18, "temp_c": 37.1, "systolic_bp": 140, "diastolic_bp": 90,
     "weight_lbs": 188.8, "steps": 1420, "sleep_hours": 5.5, "sleep_interruptions": 5},
    {"day": 7, "date": "2026-06-20", "hr_resting_bpm": 78, "hr_avg_bpm": 86, "spo2_pct": 94,
     "rr_breaths_per_min": 18, "temp_c": 37.2, "systolic_bp": 144, "diastolic_bp": 92,
     "weight_lbs": 190.2, "steps": 980, "sleep_hours": 5.0, "sleep_interruptions": 6},
]

_james_reports = [
    {"day": 1, "text": "Feeling okay today, a bit tired from the hospital stay. Took all my pills. Slept okay. Not doing much but I think that's expected."},
    {"day": 2, "text": "Little better today. Walked around the living room a few times. Appetite coming back. Ankles look normal to me."},
    {"day": 3, "text": "Pretty good day. Went to the kitchen a few times without stopping. Still a little short of breath on the stairs but manageable."},
    {"day": 4, "text": "Feeling a bit more tired than yesterday. My ankles look a little puffy this evening but I think I was just sitting too long."},
    {"day": 5, "text": "Woke up once in the night, felt a little breathless when I got up. Ankle swelling still there in the morning but went down by afternoon."},
    {"day": 6, "text": "Not a great day. Swelling in my ankles didn't really go away. Got short of breath walking to the bathroom. Skipped my afternoon walk. Woke up twice last night."},
    {"day": 7, "text": "Woke up three times last night to use the bathroom. Ankles are puffy again this morning. Really tired — had to sit down after walking to the kitchen. A little short of breath even just sitting. Not feeling right today."},
]

# ─── Patient 2: Eleanor Park — Post-TKA, Day 7, LOW-MEDIUM risk ──────────────
# Scenario: Surgical site infection developing. Temp trend approaching 38°C.
# NEWS2=0 (all vitals fine), but fever trend + wound symptoms signal early SSI.

_eleanor_profile = {
    "id": "PT-3892",
    "name": "Eleanor Park",
    "age": 68,
    "sex": "Female",
    "diagnosis": "Left Total Knee Arthroplasty (LTKA) for Osteoarthritis",
    "primary_condition": "post_surgical",
    "discharge_date": "2026-06-13",
    "medications": ["Oxycodone 5mg PRN", "Celecoxib 200mg BID", "Aspirin 81mg QD", "Lisinopril 10mg QD"],
    "baseline_hr_bpm": 72,
    "baseline_spo2_pct": 98,
    "baseline_weight_lbs": 162.0,
    "risk_factors": ["Hypertension", "Type 2 Diabetes (well-controlled)", "Obesity (BMI 31)"],
    "30day_readmission_risk": "Moderate (Risk Score: 4/10)",
}

_eleanor_history = [
    {"day": 1, "date": "2026-06-14", "hr_resting_bpm": 73, "hr_avg_bpm": 78, "spo2_pct": 98,
     "rr_breaths_per_min": 14, "temp_c": 36.8, "systolic_bp": 128, "diastolic_bp": 78,
     "weight_lbs": 162.0, "steps": 185, "sleep_hours": 6.5, "sleep_interruptions": 3},
    {"day": 2, "date": "2026-06-15", "hr_resting_bpm": 73, "hr_avg_bpm": 79, "spo2_pct": 98,
     "rr_breaths_per_min": 14, "temp_c": 36.9, "systolic_bp": 126, "diastolic_bp": 78,
     "weight_lbs": 162.2, "steps": 220, "sleep_hours": 6.8, "sleep_interruptions": 3},
    {"day": 3, "date": "2026-06-16", "hr_resting_bpm": 74, "hr_avg_bpm": 79, "spo2_pct": 97,
     "rr_breaths_per_min": 15, "temp_c": 37.0, "systolic_bp": 128, "diastolic_bp": 80,
     "weight_lbs": 162.0, "steps": 350, "sleep_hours": 7.0, "sleep_interruptions": 2},
    {"day": 4, "date": "2026-06-17", "hr_resting_bpm": 74, "hr_avg_bpm": 80, "spo2_pct": 97,
     "rr_breaths_per_min": 15, "temp_c": 37.2, "systolic_bp": 130, "diastolic_bp": 82,
     "weight_lbs": 162.4, "steps": 420, "sleep_hours": 6.5, "sleep_interruptions": 3},
    {"day": 5, "date": "2026-06-18", "hr_resting_bpm": 75, "hr_avg_bpm": 81, "spo2_pct": 97,
     "rr_breaths_per_min": 16, "temp_c": 37.4, "systolic_bp": 132, "diastolic_bp": 84,
     "weight_lbs": 162.6, "steps": 380, "sleep_hours": 6.0, "sleep_interruptions": 4},
    {"day": 6, "date": "2026-06-19", "hr_resting_bpm": 75, "hr_avg_bpm": 81, "spo2_pct": 97,
     "rr_breaths_per_min": 16, "temp_c": 37.5, "systolic_bp": 134, "diastolic_bp": 84,
     "weight_lbs": 162.8, "steps": 290, "sleep_hours": 5.8, "sleep_interruptions": 4},
    {"day": 7, "date": "2026-06-20", "hr_resting_bpm": 76, "hr_avg_bpm": 82, "spo2_pct": 97,
     "rr_breaths_per_min": 17, "temp_c": 37.7, "systolic_bp": 136, "diastolic_bp": 86,
     "weight_lbs": 163.0, "steps": 250, "sleep_hours": 5.5, "sleep_interruptions": 5},
]

_eleanor_reports = [
    {"day": 1, "text": "Knee is sore but they said to expect that. Using ice packs every two hours. Did my ankle pumps and quad sets like PT showed me. Glad to be home."},
    {"day": 2, "text": "PT came today, did the exercises. Knee is pretty swollen but therapist said that's normal for Day 2. Pain pills are helping. Managing okay."},
    {"day": 3, "text": "Noticed the swelling looks a little more than yesterday. Still doing all my exercises. A bit more tired today. Some redness around the incision but I think that's normal."},
    {"day": 4, "text": "The skin around the knee feels warm to the touch. Still swollen. Called the nurse line, they said to keep icing and monitoring. My PT session was hard today."},
    {"day": 5, "text": "I'm a bit worried — the redness has gotten bigger since yesterday. Still warm. Think I might be running a low fever. Exercises really hurt today, more than before."},
    {"day": 6, "text": "Redness is definitely spreading around the incision. The discharge paperwork says redness spreading is a warning sign. Left a message with the surgeon's office. Harder to sleep."},
    {"day": 7, "text": "Still haven't heard back from the office. Knee is red, hot, and more swollen than ever. My daughter thinks it looks infected. I didn't do my PT exercises today — it hurt too much even to try."},
]

# ─── Patient 3: Robert Chen — Post-COPD Exacerbation, Day 7, MEDIUM-HIGH ─────
# Scenario: Sub-acute respiratory deterioration. SpO2 hits ≤91% on Day 7 → NEWS2=5, Medium risk.
# Rescue inhaler escalation (1→5 puffs/day) is the key behavioral signal.

_robert_profile = {
    "id": "PT-5163",
    "name": "Robert Chen",
    "age": 61,
    "sex": "Male",
    "diagnosis": "Moderate-Severe COPD Exacerbation (GOLD Stage III)",
    "primary_condition": "copd",
    "discharge_date": "2026-06-13",
    "medications": ["Tiotropium 18mcg inhaler QD", "Salmeterol/Fluticasone 50/500 BID",
                    "Albuterol MDI PRN", "Prednisone 20mg (Day 5 of 5-day taper)"],
    "baseline_hr_bpm": 82,
    "baseline_spo2_pct": 94,
    "baseline_weight_lbs": 183.0,
    "risk_factors": ["40 pack-year smoking history (quit 2018)", "Hypertension", "Prior COPD hospitalization (2024)"],
    "30day_readmission_risk": "High (DECAF Score: 3/6 — moderate-severe)",
}

_robert_history = [
    {"day": 1, "date": "2026-06-14", "hr_resting_bpm": 82, "hr_avg_bpm": 88, "spo2_pct": 94,
     "rr_breaths_per_min": 18, "temp_c": 37.0, "systolic_bp": 138, "diastolic_bp": 86,
     "weight_lbs": 183.0, "steps": 1200, "sleep_hours": 6.5, "sleep_interruptions": 2},
    {"day": 2, "date": "2026-06-15", "hr_resting_bpm": 83, "hr_avg_bpm": 89, "spo2_pct": 94,
     "rr_breaths_per_min": 18, "temp_c": 37.1, "systolic_bp": 136, "diastolic_bp": 84,
     "weight_lbs": 183.0, "steps": 1650, "sleep_hours": 7.0, "sleep_interruptions": 2},
    {"day": 3, "date": "2026-06-16", "hr_resting_bpm": 84, "hr_avg_bpm": 90, "spo2_pct": 93,
     "rr_breaths_per_min": 19, "temp_c": 37.1, "systolic_bp": 138, "diastolic_bp": 86,
     "weight_lbs": 183.2, "steps": 1400, "sleep_hours": 6.5, "sleep_interruptions": 3},
    {"day": 4, "date": "2026-06-17", "hr_resting_bpm": 85, "hr_avg_bpm": 91, "spo2_pct": 93,
     "rr_breaths_per_min": 20, "temp_c": 37.2, "systolic_bp": 140, "diastolic_bp": 88,
     "weight_lbs": 183.0, "steps": 1200, "sleep_hours": 6.0, "sleep_interruptions": 3},
    {"day": 5, "date": "2026-06-18", "hr_resting_bpm": 87, "hr_avg_bpm": 93, "spo2_pct": 92,
     "rr_breaths_per_min": 21, "temp_c": 37.2, "systolic_bp": 142, "diastolic_bp": 88,
     "weight_lbs": 183.2, "steps": 950, "sleep_hours": 5.8, "sleep_interruptions": 4},
    {"day": 6, "date": "2026-06-19", "hr_resting_bpm": 88, "hr_avg_bpm": 94, "spo2_pct": 92,
     "rr_breaths_per_min": 22, "temp_c": 37.3, "systolic_bp": 144, "diastolic_bp": 90,
     "weight_lbs": 183.0, "steps": 700, "sleep_hours": 5.2, "sleep_interruptions": 4},
    {"day": 7, "date": "2026-06-20", "hr_resting_bpm": 90, "hr_avg_bpm": 96, "spo2_pct": 91,
     "rr_breaths_per_min": 23, "temp_c": 37.4, "systolic_bp": 146, "diastolic_bp": 90,
     "weight_lbs": 183.2, "steps": 480, "sleep_hours": 5.0, "sleep_interruptions": 5},
]

_robert_reports = [
    {"day": 1, "text": "Happy to be home. Using all my inhalers as instructed. Still short of breath but expected after being in hospital. Can walk around the house slowly."},
    {"day": 2, "text": "Went outside for a short walk, maybe 10 minutes around the block. Had to use my rescue inhaler when I got back but I pushed through it. Feeling cautiously optimistic."},
    {"day": 3, "text": "Walk today was shorter than yesterday — felt more winded. Used the rescue inhaler twice. Mostly watching TV. Stairs are very hard."},
    {"day": 4, "text": "Stayed inside today. Used the rescue inhaler three times. Getting winded just making lunch in the kitchen. This doesn't feel like improvement."},
    {"day": 5, "text": "Breathing feels worse than when I was in the hospital last week. Used rescue inhaler four times. Hard to sleep — keep waking up coughing. Very worried."},
    {"day": 6, "text": "Really bad night. Had to sit up to breathe easier. Used the rescue inhaler four more times in the night and morning. Can't walk to the mailbox. Didn't sleep much."},
    {"day": 7, "text": "Used rescue inhaler five times today and it's only noon. Can barely get from bed to the bathroom without stopping to catch my breath. This is how it felt right before I went to the hospital last time. I'm scared."},
]

_maria_profile = {
    "id": "PT-8427",
    "name": "Maria Gonzalez",
    "age": 74,
    "sex": "Female",
    "diagnosis": "Community-Acquired Pneumonia",
    "primary_condition": "post_pneumonia",
    "discharge_date": "2026-06-13",
    "medications": [
        "Amoxicillin-Clavulanate 875mg BID",
        "Albuterol inhaler PRN",
        "Amlodipine 5mg QD",
        "Metformin 500mg BID"
    ],
    "baseline_hr_bpm": 72,
    "baseline_spo2_pct": 97,
    "baseline_weight_lbs": 154.0,
    "risk_factors": [
        "Age >70",
        "Type 2 Diabetes",
        "Hypertension",
        "Recent hospitalization"
    ],
    "30day_readmission_risk": "Moderate-High"
}

_maria_history = [
    {"day": 1, "date": "2026-06-14", "hr_resting_bpm": 73, "hr_avg_bpm": 78, "spo2_pct": 97,
     "rr_breaths_per_min": 15, "temp_c": 36.8, "systolic_bp": 132, "diastolic_bp": 78,
     "weight_lbs": 154.0, "steps": 1600, "sleep_hours": 7.2, "sleep_interruptions": 1},

    {"day": 2, "date": "2026-06-15", "hr_resting_bpm": 74, "hr_avg_bpm": 79, "spo2_pct": 97,
     "rr_breaths_per_min": 15, "temp_c": 36.9, "systolic_bp": 130, "diastolic_bp": 80,
     "weight_lbs": 154.2, "steps": 1800, "sleep_hours": 7.0, "sleep_interruptions": 1},

    {"day": 3, "date": "2026-06-16", "hr_resting_bpm": 76, "hr_avg_bpm": 81, "spo2_pct": 96,
     "rr_breaths_per_min": 16, "temp_c": 37.1, "systolic_bp": 132, "diastolic_bp": 80,
     "weight_lbs": 154.0, "steps": 1750, "sleep_hours": 6.8, "sleep_interruptions": 2},

    {"day": 4, "date": "2026-06-17", "hr_resting_bpm": 79, "hr_avg_bpm": 84, "spo2_pct": 95,
     "rr_breaths_per_min": 17, "temp_c": 37.5, "systolic_bp": 134, "diastolic_bp": 82,
     "weight_lbs": 154.0, "steps": 1450, "sleep_hours": 6.3, "sleep_interruptions": 3},

    {"day": 5, "date": "2026-06-18", "hr_resting_bpm": 82, "hr_avg_bpm": 87, "spo2_pct": 94,
     "rr_breaths_per_min": 18, "temp_c": 37.8, "systolic_bp": 136, "diastolic_bp": 84,
     "weight_lbs": 154.2, "steps": 1100, "sleep_hours": 5.9, "sleep_interruptions": 4},

    {"day": 6, "date": "2026-06-19", "hr_resting_bpm": 86, "hr_avg_bpm": 91, "spo2_pct": 93,
     "rr_breaths_per_min": 20, "temp_c": 38.1, "systolic_bp": 138, "diastolic_bp": 84,
     "weight_lbs": 154.0, "steps": 800, "sleep_hours": 5.2, "sleep_interruptions": 5},

    {"day": 7, "date": "2026-06-20", "hr_resting_bpm": 90, "hr_avg_bpm": 96, "spo2_pct": 92,
     "rr_breaths_per_min": 22, "temp_c": 38.4, "systolic_bp": 140, "diastolic_bp": 86,
     "weight_lbs": 154.1, "steps": 450, "sleep_hours": 4.8, "sleep_interruptions": 6}
]

_maria_reports = [
    {"day": 1, "text": "Feeling much better than when I was in the hospital. Still coughing some but able to walk around the house."},

    {"day": 2, "text": "Appetite is coming back. Took all my medications. Cough is still there but not too bad."},

    {"day": 3, "text": "Had a little more coughing today. Felt tired after making lunch but otherwise okay."},

    {"day": 4, "text": "Feeling more tired again. Needed to rest after showering. Cough seems deeper than before."},

    {"day": 5, "text": "Didn't sleep well because I kept coughing. Bringing up more mucus than before and it looks darker."},

    {"day": 6, "text": "Very tired today. Felt feverish and spent most of the day in bed. Short of breath walking to the bathroom."},

    {"day": 7, "text": "Woke up shaking with chills last night. Coughing up thick yellow mucus. I get out of breath just walking across the room and feel worse than when I left the hospital."}
]


# ─── Unified patients dict ────────────────────────────────────────────────────

PATIENTS = {
    "PT-7421": {
        "profile": _james_profile,
        "sensor_history": _james_history,
        "self_reports": _james_reports,
    },
    "PT-3892": {
        "profile": _eleanor_profile,
        "sensor_history": _eleanor_history,
        "self_reports": _eleanor_reports,
    },
    "PT-5163": {
        "profile": _robert_profile,
        "sensor_history": _robert_history,
        "self_reports": _robert_reports,
    },
    "PT-8427": {
        "profile": _maria_profile,
        "sensor_history": _maria_history,
        "self_reports": _maria_reports,
    },
}

# ─── Backward-compat exports (used by the original main.py pattern) ──────────

PATIENT_PROFILE = _james_profile
SENSOR_HISTORY = _james_history
CURRENT_READINGS = _james_history[-1]
DAILY_SELF_REPORTS = _james_reports
CURRENT_SELF_REPORT = _james_reports[-1]["text"]
