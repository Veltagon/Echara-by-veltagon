"""ECHARA M4 agents + deterministic pipeline stages.

Three agents exactly (planner, builder, verifier) per the M4 spec. repairs.py
is NOT an agent — it is the deterministic REPAIR phase that runs between BUILD
and VERIFY; it lives here because it is part of the same pipeline.
"""
