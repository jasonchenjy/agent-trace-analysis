# Agent trace analysis

A small, deterministic implementation of **Trace Analysis** and **Failure
Classification / Grouping**. Runtime uses only the Python standard library
(Python 3.10+). There is no task generation, eval runner, grader, or LLM integration.

## Run

```sh
python -m src.cli analyze --input data/traces.jsonl --output outputs/
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
unknown/empty when the source provides no explicit association. The analyzer
preserves final text but does not infer claims from prose.

Unexpected schema fields, duplicate JSON keys, non-finite numbers, duplicate
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
| `network_permission_denied` | Permission/access denial or blocking language together with network, socket, connection, egress, or HTTP URL language |
| `invalid_tool_arguments` | Invalid arguments, unexpected keyword argument, missing required argument, or argument validation failure |
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
| `successful_recovery` | A successful call for the same operation **started after** a failed result was observed |
| `unsupported_success_claim` | An explicit success claim directly cites at least one errored result as supporting evidence |
| `unknown` | None of the above patterns is established |

Exact failed command strings are also grouped separately, without trimming or
shell normalization. This observation compares `arguments.command` strings;
operation grouping additionally uses tool identity. Two failed attempts followed
by success can carry both repetition and recovery labels. One transient failure
followed by success is recovery without repeated failure. These labels describe
observable patterns; they do not judge whether retrying was appropriate.

Final outcome assessments are `reported_incomplete`,
`tool_supported_success_claim`, `contradicted_success_claim`, or `unknown`.
Tool-supported success means the explicitly cited results all succeeded; it
does **not** verify that the user's objective was met. Uncited errors do not
contradict success, and no tools/no citations do not establish unsupported
success. A tool error alone never marks a session failed.

## Outputs and interfaces

- `trace_analysis.jsonl`: one record per valid session, including `schema_version`,
  trace ID, user request, source line, observations, classification, label-specific
  evidence indices, and capability-gap candidates.
- `failure_clusters.json`: independent `error_families` and `behavior_patterns`
  groups. Every member includes trace ID, source line, and supporting event indices.
  Recovered sessions can belong to error-family groups. The filename does not mean
  every member is a failed session. An unknown behavior can have empty indices when
  there is no final event and no positive pattern evidence.
- `validation_errors.jsonl`: source path, 1-based line number, trace ID when
  readable, and a precise validation message. Bad records never become clusters.

The CLI overwrites these output files on each run. It continues past invalid lines
and exits **0** on clean input, **1** when records were rejected, or **2** for
command/I/O errors. Blank lines count as invalid records. Output is deterministic
for identical input and input path, with no timestamps or generated metadata.

| Module | Interface |
| --- | --- |
| `src.schema` | `parse_trace(record) -> Trace`; `read_jsonl(path) -> Iterator[ReadResult]` |
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
different tools and alternative ways to achieve a task are not linked. A tool
success establishes execution evidence, not semantic correctness or causality.
Unsupported success detection deliberately misses unstructured or unlinked
claims rather than treating missing evidence as proof of a false claim.

Raw provider trace adapters are outside this implementation. No environment,
permission scope, tool availability, or capability metadata is invented.
The CLI holds analyses in memory for clustering and writes files non-atomically;
large-scale streaming/storage and transactional output handling are future work.
