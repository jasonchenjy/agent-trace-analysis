"""Pure finite-state interpreter. Proposal data is never executable code."""
from copy import deepcopy

from ..proposal_contract import validate_contract
from ..proposal_schema import agent_task
from ..review_gate import require


def arguments(call):
    return {a["name"]: a["value"] for a in call["arguments"]}


class FiniteStateEnvironment:
    def __init__(self, proposal):
        validate_contract(proposal)
        self._model = deepcopy(proposal["eval_private"]["environment_model"])
        self._public = deepcopy(agent_task(proposal))
        self._state = self._model["initial_state"]
        self._states = {s["state_id"]: s for s in self._model["states"]}
        self._fixtures = {f["fixture_id"]: f for f in self._model["fixtures"]}
        self._operations = {(t["name"], o["name"]): o for t in self._model["tools"] for o in t["operations"]}

    @property
    def state(self):
        return self._state

    @property
    def max_steps(self):
        return self._model["max_steps"]

    @property
    def fixture_ids(self):
        return list(self._states[self.state]["fixture_ids"])

    @property
    def goal(self):
        return (self.state in self._model["terminal"]["success_states"] and
                set(self._model["goal"]["required_fixture_ids"]) <= set(self.fixture_ids))

    @property
    def terminal(self):
        return self.state in (self._model["terminal"]["success_states"] + self._model["terminal"]["failure_states"])

    def _response(self, response):
        # IDs and private fixture metadata never cross the public boundary.
        return {"code": response["code"], "feedback": response["feedback"], "files": [
            {"path": self._fixtures[f]["path"], "content": self._fixtures[f]["content"]}
            for f in response["fixture_ids"]
            if f in self.fixture_ids and self._fixtures[f]["visibility"] == "agent_visible"]}

    def public_context(self):
        context = deepcopy(self._public)
        cp = self._model["checkpoint"]
        context["historical_tool_exchange"] = {
            "request": {"tool": cp["tool"], "operation": cp["operation"], "arguments": arguments(cp)},
            "result": self._response(cp["response"])}
        return context

    def valid_request(self, request):
        if type(request) is not dict or set(request) != {"tool", "operation", "arguments"}:
            return False
        if type(request["tool"]) is not str or type(request["operation"]) is not str:
            return False
        op = self._operations.get((request["tool"], request["operation"]))
        supplied = request["arguments"]
        if op is None or type(supplied) is not dict or set(supplied) != {a["name"] for a in op["arguments"]}:
            return False
        return all(type(supplied[a["name"]]) is {"string": str, "integer": int}[a["value_type"]]
                   and supplied[a["name"]] in a["allowed_values"] for a in op["arguments"])

    def execute(self, request):
        require(not self.terminal, "environment is terminal")
        before = self.state
        transition_id = None
        if not self.valid_request(request):
            response = {"code": "INVALID_REQUEST", "feedback": "Use a declared tool and operation with exactly its typed arguments and allowed values.", "files": []}
            tag = "invalid_request"
        else:
            op = self._operations[request["tool"], request["operation"]]
            match = next((t for t in self._model["transitions"]
                          if t["from_state"] == self.state and t["tool"] == request["tool"]
                          and t["operation"] == request["operation"] and arguments(t) == request["arguments"]), None)
            if match:
                self._state = match["to_state"]
                transition_id = match["transition_id"]
                response, tag = self._response(match["response"]), match["event"]
            else:
                response, tag = self._response(op["default_response"]), op["default_event"]
        return response, {"state_before": before, "state_after": self.state,
                          "fixture_ids": self.fixture_ids, "event_tag": tag, "transition_id": transition_id}


class ToolGateway:
    def __init__(self, environment, max_steps=None):
        self._environment = environment
        require(max_steps is None or type(max_steps) is int and max_steps > 0, "max_steps must be a positive integer")
        self.max_steps = min(max_steps or environment.max_steps, environment.max_steps)
        self._events = []

    @property
    def events(self):
        return deepcopy(self._events)

    @property
    def stop_reason(self):
        if self._environment.terminal:
            return "terminal_success" if self._environment.goal else "terminal_failure"
        if len(self._events) >= self.max_steps:
            return "step_limit"
        return None

    def dispatch(self, request):
        require(self.stop_reason is None, "tool execution closed")
        # Environment validates again so callers cannot bypass its invariants.
        self._environment.valid_request(request)
        response, internal = self._environment.execute(request)
        self._events.append({"step": len(self._events) + 1, "request": deepcopy(request),
                             "result": deepcopy(response), **internal})
        return deepcopy(response)
