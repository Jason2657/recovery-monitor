"""recovery_monitor — post-surgery recovery trend-and-triage early-warning tool.

A decision-support pipeline (NOT a diagnostic device) that watches a home
recovery sensor stream + daily self-reports, runs specialized agents that argue
before bothering a clinician, and escalates with a proper clinical handoff.

See ``docs/architecture.md`` for the message flow and the sponsor-boundary map
(Fetch.ai builds the agents, Band governs the decision, Arize observes + corrects).
"""

__version__ = "0.1.0"
