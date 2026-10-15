# Agent trace analysis

A small, deterministic implementation of **Trace Analysis** and **Failure
Classification / Grouping**. Runtime uses only the Python standard library
(Python 3.10+). There is no task generation, eval runner, grader, or LLM integration.

## Run

```sh
python -m src.cli analyze --input data/candidate_traces.jsonl --output outputs/
# Original normalized synthetic fixtures:
python -m src.cli analyze --input data/traces.jsonl --output /tmp/trace-synthetic
```

If your system exposes Python as `python3`, use that command instead.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'pytest>=8,<10'
python -m pytest -q
```

`data/traces.jsonl` contains 13 synthetic valid sessions. Deliberately malformed
examples are in `data/invalid_traces.jsonl`. Test expectations live only in
`tests/expected_labels.json`; the pipeline never reads them.

`data/candidate_traces.jsonl` is an unchanged copy of the 30 supplied candidate
sessions from `../agent_trace_eval_data/traces.jsonl`. The reviewer gold reference
is not copied, opened, or used by the pipeline or these regression tests.

## Raw input adapter

The CLI defaults to `--input-format auto`; `raw` and `normalized` can be selected
explicitly. `src.adapter.adapt_raw_trace()` maps the supplied session schema:

| Raw field | Normalized representation |
| --- | --- |
| `tool_call.command` | `arguments.command`, unchanged |
| `tool_result.exit_code` | execution `status`: zero → success, nonzero → error, null → unknown |
| `tool_result.stdout`, `stderr` | combined `output` with a separating newline when necessary |
| `assistant_final.content` | `text`, unchanged |

Call IDs, tools, event order, and supplied event indices are retained. Where the
raw source has no indices, zero-based array positions provide evidence locations.
No events are reordered. The complete decoded input object is copied to
`source_record` and emitted in the analysis output, preserving separate streams,
original content, and all unknown fields. This is value preservation, not JSON
whitespace/escape preservation. Extensions remain inert: they are not promoted
to operation IDs, outcome labels, evidence links, or environment metadata.
The adapter never populates `claimed_outcome` or `evidence_call_ids`.

Raw records still validate call/result pairing, ordering, types and final-event
placement using the existing normalized validator. A raw event requires its
schema fields; missing fields are errors, not silently synthesized content.
Empty raw sessions without distinctive fields require `--input-format raw` if
source preservation is needed. Malformed records produce per-line errors.

## Normalized input contract

UTF-8 JSONL, one complete session object per line:

```json
{
  "trace_id": "example",
  "user_request": "Run the tests",
  "events": [
    {"event_index": 4, "type": "tool_call", "call_id": "c1", "tool": "shell", "operation": "run_tests", "arguments": {"command": "pytest"}},
    {"event_index": 7, "type": "tool_result", "call_id": "c1", "status": "error", "output": "1 failed", "exit_code": 1},
    {"event_index": 9, "type": "assistant_final", "text": "Verification failed.", "claimed_outcome": "incomplete", "evidence_call_ids": ["c1"]}
  ]
}
```

The example is pretty-printed for readability; serialize it onto one line.

| Record | Required fields | Optional fields |
| --- | --- | --- |
| Session | `trace_id`: nonempty string; `user_request`: nonempty string; `events`: array | None |
| Every event | `event_index`: nonnegative integer; `type`: supported event type | See below |
| `tool_call` | `call_id`: nonempty string; `tool`: nonempty string; `arguments`: JSON object | `operation`: nonempty string |
| `tool_result` | `call_id`: nonempty string; `status`: `success`, `error`, or `unknown`; `output`: string | `exit_code`: integer |
| `assistant_final` | `text`: string | `claimed_outcome`: `success`, `incomplete`, or `unknown` (default); `evidence_call_ids`: array of completed call IDs (default `[]`) |

Indices must strictly increase, but need not be contiguous or start at zero.
They are original source positions, never reindexed by this pipeline. A call ID
is unique within its session and can have at most one result. Results must follow
their calls; interleaving is allowed. A final event, if present, must be last.
Missing results and absent final events are accepted as incomplete evidence.
An empty events array is valid and has an unknown outcome.

`status` must reflect the tool's reported execution result. A nonzero exit code
requires `error`; zero alone does not establish success (tools can report errors
in a response even when their process exits zero). Status is never inferred from
words in otherwise successful output.

`operation` is a producer-supplied identity scoped to the tool and session, such
as `read_config`. Reuse it only when attempts address the same operation. When
absent, identity uses the tool and canonical JSON arguments (object key order
ignored; array order and values retained). Explicit and inferred identities
are distinct. The analyzer does not guess semantic equivalence between commands.

`claimed_outcome` records the assistant's claim, **not a ground-truth label**.
`evidence_call_ids` identifies results offered as direct support for that claim,
not arbitrary historical/contextual citations. An adapter must leave these fields
unknown/empty when the source provides no explicit association. The normalized-input path preserves final text and requires explicit claims.
For raw input only, the classifier has limited, labeled prose rules described below;
these do not modify source claims or invent producer-supplied evidence links.

Unexpected normalized schema fields (raw extensions are preserved), duplicate JSON keys, non-finite numbers, duplicate
trace IDs, invalid types, orphan/duplicate results, and invalid references are
rejected. Tool arguments remain tool-specific JSON: e.g. an invalid `path: 7`
is a valid trace of an invalid tool invocation. The analyzer does not validate
external tools' schemas.

## Observations and classification rules

Every completed error result records the original output, optional exit code,
call ID, and call/result event indices. Message signatures assign one or more
error families:

| Error family | Evidence signature |
| --- | --- |
| `network_permission_denied` | Permission/access denial, denied-by-policy, policy-denied, EPERM/EACCES, or blocking language together with network, socket, connection, egress, or HTTP URL language |
| `invalid_tool_arguments` | Invalid arguments, unexpected keyword argument, missing required argument, argument validation failure, missing CLI option value, or invalid argument value |
| `missing_file` | No such file or directory, file not found, or FileNotFoundError |
| `test_failure` | A positive failed-test count, tests failed, AssertionError, or a `FAILED` report line |
| `unknown` | An explicit tool error without a recognized signature, including a connection timeout |

No errors yields `error_families: []`. General filesystem permission errors do
not imply a network restriction. Network denials produce `network_access`
capability-gap **candidates**, never established environmental facts.

The independent behavior dimension supports multiple labels:

| Behavior | Rule |
| --- | --- |
| `repeated_failed_operation` | At least two error results for the same operation |
| `successful_recovery` | A successful call for the same operation **started after** a failed result was observed; execution scope only |
| `unsupported_success_claim` | Claim-associated evidence contradicts success, including a zero-exit test invocation with no collected tests |
| `unknown` | None of the above patterns is established |

Exact failed command strings are also grouped separately, without trimming or
shell normalization. This observation compares `arguments.command` strings;
operation grouping additionally uses tool identity. Two failed attempts followed
by success can carry both repetition and recovery labels. One transient failure
followed by success is recovery without repeated failure. These labels describe
observable patterns; they do not judge whether retrying was appropriate.

`recoveries` retains conservative tool-scoped operation matching and explicitly
reports `scope: same_operation_execution`. `goal_recovery.status` stays
`unverified`: execution recovery does not prove the user's objective was met.
Its separate candidates cover only pnpm install commands differing by `--offline`
(with all other arguments/context retained), or a producer operation ID shared
across tools. Candidates require a later successful call to start after the error;
they never clear `unresolved_failed_call_ids`. Unlinked tool switches, path changes,
and other alternative strategies remain unassessed rather than guessed.

Final assessments distinguish `verified_success_claim`,
`contradicted_success_claim`, `unverified_success_claim`, `reported_incomplete`,
and `unknown`. An explicit success claim with absent or insufficient evidence is
unverified, not false. Cited exit code 0 alone is also unverified.

`success_claim_assessment` records the scope, claim/evidence basis, call IDs,
source indices and report findings. Verification currently supports only the
narrow predicate that an associated test invocation reports positive passes and
no failures (`scope: reported_test_execution`). It does not verify every clause
of the final message, exact prose counts, test coverage, artifact correctness,
or completion of the user's whole goal; `goal_status` remains `unverified`.

For raw input, small English prose rules recognize test-pass statements and a
few general completion verbs. Prose recognition is labeled `raw_prose_rule`.
A test claim may use an `inferred_unique_test_command` association only when the
trace contains one exact test command/context (possibly retried sequentially).
Different test commands, overlapping invocations, pending results, and unrelated
successful commands do not establish support. General completion claims remain
unverified without explicit evidence; unrecognized prose remains unknown.
Normalized traces continue to require producer-provided claims and evidence links.

A zero-exit result saying `No test files found, exiting with code 0` contradicts
an associated claim that tests ran and passed. It does not become a fabricated
tool error. Error signatures still apply only to explicit execution errors.
This keeps incidental warning text in a successful build out of error groups.

## Outputs and interfaces

- `trace_analysis.jsonl`: one record per valid session, including `schema_version`,
  trace ID, user request, source line, source format (and original raw record),
  observations, classification, label-specific
  evidence indices, and capability-gap candidates.
- `failure_clusters.json`: independent `error_families` and `behavior_patterns`
  groups. Every member includes trace ID, source line, and supporting event indices.
  The additional top-level `failure_patterns` groups by `(error_family,
  response_behavior)`. Each member carries failed call IDs and source indices.
  A behavior is attached only to the failed calls that establish it; unrelated
  session labels are never cross-multiplied. Multiple contributing failures in
  one trace count as one trace member. An unmatched response is `unknown`, not
  evidence that the agent stopped or failed. Alternative recovery candidates
  remain explicitly labeled candidates in these groups.
  Recovered sessions can belong to error-family groups. The filename does not mean
  every member is a failed session. An unknown behavior can have empty indices when
  there is no final event and no positive pattern evidence.
- `validation_errors.jsonl`: source path, 1-based line number, trace ID when
  readable, and a precise validation message. Bad records never become clusters.

Output schema version is 2 because final-outcome semantics changed. The CLI overwrites these output files on each run. It continues past invalid lines
and exits **0** on clean input, **1** when records were rejected, or **2** for
command/I/O errors. Blank lines count as invalid records. Output is deterministic
for identical input and input path, with no timestamps or generated metadata.

| Module | Interface |
| --- | --- |
| `src.schema` | `parse_trace(record) -> Trace`; `read_jsonl(path, input_format="auto") -> Iterator[ReadResult]` |
| `src.adapter` | `adapt_raw_trace(record) -> Trace`; `parse_input(record, input_format="auto") -> Trace` |
| `src.claims` | Scoped test-report claim assessment |
| `src.recovery` | Narrow alternative-operation recovery candidates |
| `src.analysis` | `analyze_trace(trace) -> dict`; `error_families(output) -> list[str]` |
| `src.classification` | `classify(observations) -> dict` |
| `src.clustering` | `group_analyses(analyses) -> dict` |
| `src.cli` | `main(argv=None) -> int` |

Library callers receive `ValidationError` for invalid records. `read_jsonl`
instead returns per-line errors so batch processing can continue.

## Limitations

The signatures are English-centric heuristics, not root-cause analysis. Quoted
error text inside an error response can match; paraphrases may be missed.
Operation matching depends on reliable producer identities or exact arguments;
different tools and alternative ways to achieve a task are not established recoveries.
The separate candidate rules cover only the narrow cases documented above. A tool
success establishes execution evidence, not semantic correctness or causality.
Success assessment deliberately leaves ambiguous or unrecognized claims unverified
or unknown rather than treating missing evidence as proof of a false claim.

Only the supplied raw schema and the normalized contract are supported. No environment,
permission scope, tool availability, or capability metadata is invented.
The CLI holds analyses in memory for clustering and writes files non-atomically;
large-scale streaming/storage and transactional output handling are future work.
