"""Arize / Phoenix observability + the self-correction loop.

``tracing`` wraps every agent step in a span (no-op when no keys are present);
``evaluator`` scores the Reconciler's risk calls and nudges the single tunable
``RISK_THRESHOLD`` knob in :mod:`recovery_monitor.config`.
"""
