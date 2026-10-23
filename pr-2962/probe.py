import sys; sys.path.insert(0, ".")
from core.workflows.custom_workflow_service import _definition_value, _normalize_phases
from core.workflows.workflows_router import WorkflowPhaseSchema, CustomWorkflowCreate
from core.workflows.workflow_ai_generator import WorkflowAIGenerator
OLD = {"phase_id":"phase-1","order":1,"agent_id":"triage","name":"T","purpose":"p","tools":[],"steps":["s"],
       "expected_output":"o","timeout_seconds":300,"approval_required":False,"conditions":None,"parallel_group":None}
new = {k:v for k,v in OLD.items() if k not in ("timeout_seconds","conditions","parallel_group")}
assert _definition_value("phases",[OLD]) == _definition_value("phases",[new]); print("old stored row == new row: OK")
odd = dict(OLD, timeout_seconds=9, conditions={"if":"x"}, parallel_group="g")
out = _normalize_phases([odd])[0]
assert not {"timeout_seconds","conditions","parallel_group"} & set(out), out; print("odd old row validates, keys dropped:", sorted(out))
body = CustomWorkflowCreate.model_validate({"name":"n","description":"d","phases":[odd]})
assert "timeout_seconds" not in body.model_dump()["phases"][0]; print("POST body with old keys accepted:", body.model_dump()["phases"][0])
g = WorkflowAIGenerator.__new__(WorkflowAIGenerator)
d = g._normalize_draft({"phases":[{"agent_id":"triage","name":"x"}]})
print("draft phase:", d["phases"][0]); assert set(d["phases"][0]) == {"agent_id","name","phase_id","order","tools","steps","approval_required"}
