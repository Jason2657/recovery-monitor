"""Skeptic agent — argues AGAINST escalation.

Owner: Person B (Debate & governance).

Its job is institutional doubt: before anyone bothers a clinician, the Skeptic
looks for the benign explanation. It is what stops the system from crying wolf
(the benign-decoy profile is designed to be caught here). It does NOT decide —
it hands a rebuttal to the Reconciler, which weighs it.
"""

from __future__ import annotations

from recovery_monitor.observability.tracing import traced
from recovery_monitor.schemas import (
    AgentFinding,
    EarlyWarningScore,
    PatientContext,
    SkepticRebuttal,
)

# Placeholder lexicon: phrases in a self-report that plausibly explain away an
# alarming reading. Replaced by real reasoning (see TODO in rebut()).
_BENIGN_CUES = (
    "travel",
    "traveled",
    "flight",
    "visitor",
    "visitors",
    "guest",
    "party",
    "wedding",
    "moved house",
    "hot weather",
    "heat wave",
    "missed my pill",
    "forgot my",
)


class SkepticAgent:
    """Builds the case against escalation."""

    name = "skeptic_agent"

    @traced("skeptic_agent.rebut")
    async def rebut(
        self,
        findings: list[AgentFinding],
        ews: EarlyWarningScore,
        context: PatientContext,
    ) -> SkepticRebuttal:
        """Argue down the findings: surface benign explanations.

        STUB: returns one plausible benign counterpoint, plus a light keyword
        heuristic over the self-reports so the benign-decoy demo actually does
        something. The keyword scan is a placeholder, not the real logic.

        TODO(governance-owner): replace the heuristic with a Claude call that
        reads the findings, the EWS, and the self-reports and produces genuine
        counterarguments (e.g. "HR bump coincides with the reported flight and
        resolves by day 6; mobility is stable — likely travel, not infection").
        Set ``suppression_strength`` from how completely the benign story accounts
        for the alarming signals.
        """
        counterpoints: list[str] = [
            "Single-reading noise and normal post-op variability can mimic "
            "deterioration; the trajectory may still be within expected recovery.",
        ]
        suppression = 0.2

        # Placeholder: does any recent self-report offer a benign explanation?
        recent_text = " ".join(r.raw_text.lower() for r in context.self_reports[-3:])
        hits = sorted({cue for cue in _BENIGN_CUES if cue in recent_text})
        if hits:
            counterpoints.append(
                "Patient self-report mentions "
                f"{', '.join(hits)} — a plausible benign cause for elevated "
                "vitals / disrupted sleep."
            )
            suppression = 0.6

        return SkepticRebuttal(
            counterpoints=counterpoints,
            suppression_strength=suppression,
        )
