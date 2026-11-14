"""Pipeline nodes: one file per stage. Each exposes STAGE, TITLE and run(...) -> (status, message, data, value).
The orchestrator (cockpit/pipeline.py) runs them in order and persists each result as an event."""
from . import decision, explanation, extraction, intake, matching, po, policy, validation, vendor
from ._shared import StageStop

NODES = (intake, extraction, validation, vendor, po, matching, policy, decision, explanation)
STAGES = tuple(n.STAGE for n in NODES)

__all__ = ["NODES", "STAGES", "StageStop"]
