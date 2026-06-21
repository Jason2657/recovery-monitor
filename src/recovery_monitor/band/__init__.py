"""Band — the governed coordination "room".

Agents reconcile here under a verified-authority gate and every consequential
step writes to an append-only audit trail. The concrete backend lives behind an
adapter (:mod:`recovery_monitor.band.adapter`) so the real Band API can be
swapped in without touching the pipeline.
"""
