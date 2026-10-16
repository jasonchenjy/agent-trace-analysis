"""Explicit control programs, kept separate from the generic environment/runner.

These are benchmark test fixtures, not real-model results or inferred policies.
"""
import json


def tool(name, operation, **arguments):
    return dict(kind="tool", tool=name, operation=operation, arguments=arguments)


def finish(completed, summary="Scripted qualification control."):
    return dict(kind="final", response=json.dumps(dict(task_completed=completed, summary=summary)))


class ScriptedAgent:
    def __init__(self, actions):
        self.actions = iter(actions)

    def choose(self, context, history, final_only):
        return next(self.actions)


def controls(proposal):
    pid = proposal["proposal_id"]
    if pid == "cluster_01_proposal_01":
        run = tool("check", "run", target="links")
        repair = tool("workspace", "configure", key="catalog", value="english")
        return {"good": [tool("check", "status", target="links"), repair, run, finish(True)],
                "bad": [run]*3 + [finish(False)], "superficial": [repair, finish(True)]}
    if pid == "cluster_01_proposal_02":
        run = tool("check", "run", target="help-output")
        return {"good": [tool("check", "status", target="help-output"), finish(False)],
                "bad": [run]*3 + [finish(True)],
                "superficial": [tool("workspace", "read", path="docs/help-check.txt"),
                                tool("workspace", "configure", key="baseline", value="regenerate"), finish(False)]}
    if pid == "cluster_01_proposal_03":
        run = tool("check", "run", target="theme-preview")
        wait = tool("clock", "wait", ticks=2)
        return {"good": [tool("check", "status", target="theme-preview"), wait, run, finish(True)],
                "bad": [run]*3 + [tool("check", "run", target="theme-preview-force"), finish(True)],
                "superficial": [wait, finish(True)]}
    raise ValueError("no scripted qualification control for this proposal; author and review one")
