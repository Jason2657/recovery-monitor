"""Specialized agents (Fetch.ai uAgents layer).

Each analysis agent subclasses :class:`recovery_monitor.agents.base.BaseAgent`
and returns an :class:`~recovery_monitor.schemas.AgentFinding`. The Skeptic and
Reconciler operate inside the Band room; the Clinician-brief and Escalation
agents act on the gate's "risk high" fork.
"""
