"""Conservative, scoped final-claim assessment from observable test reports.

Source claims/links are never rewritten. Raw prose rules and inferred report
associations are explicitly identified; arbitrary goal completion is unverified.
"""

import re
import shlex


def is_test_call(call):
    command = call["arguments"].get("command", "")
    if not isinstance(command, str) or any(c in command for c in ";|&><\n"):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]):
        words.pop(0)
    if not words:
        return False
    return (words[0] in {"pytest", "vitest", "jest"}
            or words[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"])
            or (words[0] in {"npm", "pnpm", "yarn"} and len(words) > 1 and words[1] == "test")
            or (words[0] in {"npm", "pnpm", "yarn"} and len(words) > 2
                and words[1] == "run" and words[2].split(":")[0] == "test")
            or words[:2] in (["npx", "vitest"], ["npx", "jest"]))


def test_report(result):
    text = result["output"]
    if re.search(r"^\s*(?:no test files found|no tests (?:found|ran|collected)|collected 0 items)\b", text, re.I | re.M):
        return "no_tests_collected"
    if result["status"] == "error":
        return "execution_error"
    if re.search(r"(?:^|[,\s])(?:[1-9]\d*) (?:failed|errors?)\b|^FAILED\s", text, re.I | re.M):
        return "failing_tests"
    if result["status"] == "success" and re.search(
        r"(?:^\s*(?:(?:Tests|Test Files|Test Suites)\s*:?\s*)?|,\s*)[1-9]\d* passed\b",
        text, re.I | re.M,
    ):
        return "passing_tests"
    return "unknown"


def assess_success_claim(trace, calls, results, final):
    assessment = {"status": "unknown", "scope": "final_success_claim",
                  "claim_basis": "none", "evidence_basis": "none",
                  "call_ids": [], "event_indices": [], "reports": [],
                  "goal_status": "unverified"}
    if final is None:
        return assessment
    text = final["text"]
    # Deliberately small English rule set. Unrecognized prose stays unknown.
    negative = re.search(r"\b(?:not|never|cannot|could not|didn't|failed)\b", text, re.I)
    test_claim = not negative and bool(re.search(
        r"\b(?:tests?|suites?)\b[^.!\n]*\b(?:pass(?:ed|es|ing)?|successfully)\b|"
        r"\b(?:passing tests|test suites successfully)\b", text, re.I))
    explicit = final.get("claimed_outcome") == "success"
    raw_prose = trace.source_record is not None and "claimed_outcome" not in final
    general_claim = not negative and bool(re.search(
        r"\b(?:installed|downloaded|regenerated|added|corrected|updated|fixed|succeeded|completed)\b", text, re.I))
    if not explicit and not (raw_prose and (test_claim or general_claim)):
        return assessment
    assessment.update(status="unverified", event_indices=[final["event_index"]],
                      claim_basis="explicit_claimed_outcome" if explicit else "raw_prose_rule",
                      reason="Tool execution success alone does not verify a final claim.")
    refs = final.get("evidence_call_ids", [])
    if refs:
        assessment["evidence_basis"] = "producer_evidence_call_ids"
    elif raw_prose and test_claim:
        test_calls = [c for c in calls.values() if is_test_call(c)]
        # Unlinked prose is associated only with one exact test command/context.
        # Multiple different suites are ambiguous, even if all exited zero.
        if test_calls and all(c["tool"] == test_calls[0]["tool"]
                              and c["arguments"] == test_calls[0]["arguments"] for c in test_calls):
            last = test_calls[-1]
            # Overlapping attempts cannot establish that a later call supersedes
            # an earlier report; leave such cases unverified.
            if (last["call_id"] in results and all(
                    c["call_id"] in results and results[c["call_id"]]["event_index"] < last["event_index"]
                    for c in test_calls[:-1])):
                refs = [last["call_id"]]
                assessment["evidence_basis"] = "inferred_unique_test_command"
    assessment["call_ids"] = list(refs)
    assessment["event_indices"] = sorted({final["event_index"]} | {
        index for ref in refs for index in (calls[ref]["event_index"], results[ref]["event_index"])
    })
    if any(results[ref]["status"] == "error" for ref in refs):
        assessment.update(status="contradicted", reason="Claim-associated execution reported an error.")
    if test_claim:
        assessment["scope"] = "reported_test_execution"
        reports = [{"call_id": ref, "finding": test_report(results[ref])}
                   for ref in refs if is_test_call(calls[ref])]
        assessment["reports"] = reports
        if any(r["finding"] in {"no_tests_collected", "execution_error", "failing_tests"} for r in reports):
            assessment.update(status="contradicted", reason="Test report contradicts tests having run and passed.")
        elif (reports and len(reports) == len(refs)
              and all(r["finding"] == "passing_tests" for r in reports)):
            assessment.update(status="verified", reason="Associated report records passing tests; scope is that test execution only.")
            assessment["verified_predicate"] = "Associated test invocation reported positive passes and no failures."
    return assessment
