# Agent trace analysis

A small **Agent Trace Analysis and Failure Discovery** pipeline. The deterministic
baseline remains available; a separate LLM-assisted path discovers candidate
behavioral patterns without a predefined taxonomy. Runtime uses only the Python
standard library for the baseline (Python 3.10+); discovery uses the optional
`openai-codex` SDK. Pipeline 2 constructs human-reviewable evaluation proposals
from approved discoveries. No agent runner, environment simulator, executable
grader, model comparison, embedding database, or dashboard is implemented.

## Run the deterministic baseline

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

## Baseline observations and classification rules

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

## Baseline outputs and interfaces

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

## Baseline limitations

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

## Discovery: neutral evidence → per-trace analysis → candidate taxonomy

```text
read_jsonl / raw adapter (existing)
  → extract_observations (no baseline labels)
  → per-trace LLM analysis
  → schema + source evidence validation
  → cross-trace LLM grouping
  → schema + membership + source evidence validation
  → candidate taxonomy and human review
```

The baseline analyzer/classifier is unchanged and is not called by discovery.
`src.observations` exposes user requests, original event indices and call IDs,
tool arguments, statuses, exit codes, outputs, separate raw stdout/stderr, final
text, exact repeated calls, argument differences between consecutive calls to the
same tool, and missing results/final responses. Consecutive calls are not assumed
to address the same operation or goal. No error-family labels, baseline outcomes,
producer outcome annotations, or reviewer metadata enter discovery prompts.

The complete original record remains in the local per-session output for review;
only allowlisted neutral fields enter the LLM request. Tool arguments and outputs
are still untrusted source data. The two prompts contain no example failure
categories or fixed pattern names. Trace text and first-pass analyses are sent as
user-message data; thread base instructions prohibit following embedded
instructions or assuming hidden environment facts. Each request uses a fresh,
ephemeral Codex thread in an empty temporary working directory with
`Sandbox.read_only` and `ApprovalMode.deny_all`. Configuration overrides disable
shell execution, web search, apps, multi-agent use and configured MCP servers.
The repository and reviewer files are not supplied as workspace context. Read-only
mode restricts writes, not all filesystem reads; these controls and prompts are
not a proof of resistance to every malicious trace instruction.

### Real Codex SDK run

Install the pinned optional dependency and sign in/configure Codex for the model
you want to use. Authentication is handled by Codex; the adapter does not require
or inspect `OPENAI_API_KEY`, and does not call the Responses HTTP endpoint.

```sh
.venv/bin/python -m pip install openai-codex==0.162.1
# Complete Codex login/configuration in your local environment first.
export CODEX_MODEL="YOUR_MODEL_NAME"
.venv/bin/python -m src.cli discover \
  --input data/candidate_traces.jsonl \
  --output outputs/discovery/ \
  --model "$CODEX_MODEL"
```

`pyproject.toml` also declares the SDK under the `discovery` extra. The baseline
and fake-client tests do not require the SDK. The installed SDK API is checked
by a signature compatibility test (skipped when the optional package is absent).

The adapter follows the [Codex Python SDK](https://learn.chatgpt.com/docs/codex-sdk)
workflow:

```python
from openai_codex import Codex, Sandbox

with Codex() as codex:
    thread = codex.thread_start(model=model, sandbox=Sandbox.read_only)
    result = thread.run(prompt, output_schema=schema)
    response_text = result.final_response
```

The implementation additionally sets an isolated working directory, ephemeral
threads, noninteractive approval denial and analysis instructions. Model names
are passed through from `--model`; no model is hardcoded. `output_schema` supplies
the existing JSON contract, followed by the same Python evidence validation.
Unsuccessful/interrupted turns, missing final responses, malformed JSON and invalid
references are rejected. The adapter does not silently fall back to direct HTTP.

`--max-attempts` defaults to 3 (maximum 5), with bounded exponential backoff for
SDK overload, closed transport, connection and timeout exceptions. Every attempt
creates and closes its own Codex context and thread. Other SDK/runtime errors
stop subsequent requests and report an actionable installation/login/model-access
error without copying raw runtime diagnostics. Failed turns are reported while
other traces can continue. SDK/runtime internal retries are separate from this
adapter budget.

The former HTTP-only `--json-mode`, `--timeout`, and `--max-output-tokens` options
were removed: this SDK's synchronous `thread.run()` has no equivalent parameters.
There is currently no adapter-enforced wall-clock deadline; turn limits follow
the Codex runtime. `--input-format` and `--cache-dir` remain available.

### Analysis and evidence contract

`src.discovery_schema` defines the API JSON schemas and validates them in Python.
Per-trace results contain goal interpretation, execution summary, proposed observed
facts, obstacle, agent response, outcome reasoning, missing information and freely
named candidate patterns. Broad assessments are `potential_failure`,
`successful_behavior`, and `unclear`; outcomes are `achieved`, `not_achieved`, or
`unknown`. These statuses do not enumerate failure categories. A tool error is
not automatically an agent failure, and exit 0 is not task-ground-truth evidence.

Every pattern and proposed observed fact needs evidence. Each evidence item has:

```json
{"event_index": 3, "call_id": "c2", "field": "/output", "quote": "exact source excerpt"}
```

`field` is a JSON pointer into a neutral event; `quote` must be a nonempty exact
substring of that scalar field, at most 300 characters. The event must exist in
the specified session and its call ID must match (`null` for final events).
Unknown indices, fabricated quotes, mismatched sessions/calls, missing fields,
duplicate IDs, duplicate JSON keys and non-finite values are rejected. Missing
evidence in summaries/obstacle/response/unknown outcome must be acknowledged in
`unknowns`; achieved/not-achieved outcomes require evidence. User-goal text is
an interpretation of the supplied request, not independent ground truth.

The second pass receives validated per-trace results only. Each cluster defines
a dynamic name, common behavior, candidate capability gap, members, representative
example and uncertainties. Members reference existing `potential_failure` pattern
IDs and reuse their validated evidence. Each recurring cluster needs at least two
distinct sessions; singletons remain accessible as ungrouped findings. Multiple
memberships are allowed. Successful/unclear patterns cannot be silently included
in failure clusters. Every cluster is `needs_review`. An invalid taxonomy response
is rejected as a unit, while valid per-trace discoveries are retained.

**Reference validation is not semantic validation.** A real quotation can still
be misinterpreted, omit counterevidence, or fail to support a capability-gap claim.
All proposed facts, interpretations, outcomes and cluster boundaries need human
review; there is no grader in this stage.

### Discovery artifacts and cache

| File | Contents |
| --- | --- |
| `trace_discoveries.jsonl` | Valid per-trace analyses, neutral observations, original records, source line, model/prompt identity, cache key and review status |
| `discovered_taxonomy.json` | Candidate clusters, evidence, representative members, ungrouped patterns, sessions without patterns and completion status |
| `discovery_review.md` | Human-readable clusters plus every analyzed session, quoted evidence, successful/ambiguous references, uncertainties and errors |
| `discovery_errors.jsonl` | Input, trace-analysis or taxonomy errors with trace/source context; rejected output is not promoted to a finding |
| `run_metadata.json` | Requested model/provider, prompt and validation versions, SHA-256 of the exact input dataset, counts, cache hits and run/taxonomy status |

Successful responses are cached under `outputs/discovery/cache/`, or a chosen
`--cache-dir`. Keys include provider/configuration, Codex SDK version, requested model, full prompt
and prompt version, schema, validation version and canonical relevant input hash.
The taxonomy key includes the dataset hash and all included validated analyses.
Fake, Codex, and previous Responses clients use different cache namespaces. Cache hits are revalidated
against source evidence. Corrupted entries cause a reported error; remove that
entry to retry. Delete the cache or use another directory for fresh generations.

Parsed inputs, validation and grouping membership checks are deterministic.
Uncached model generations are not guaranteed to repeat; a model alias may also
change upstream. Caches preserve accepted prior responses, not model determinism.
Cache files and artifacts contain trace data and should be handled like the input.
Each discovery file is replaced atomically, but the output set is not a transaction;
concurrent writers to the same output directory are unsupported.

Exit codes: **0** for a fully valid run, **1** for partial/validation failures,
**2** for fatal client configuration or command/I/O errors. Missing SDK/runtime dependencies and Codex login/model-access failures
produce explicit errors and no fabricated discoveries. If no validated traces are
available, taxonomy status is `skipped`, not a claimed discovery of zero failures.

### Tests and current validation

```sh
.venv/bin/python -m pytest -q
# Full 30-session integration using a clearly identified scripted fake client:
.venv/bin/python -m pytest -q tests/test_discovery.py::test_all_30_candidates_fake_integration_and_cache
```

The fake client is test-only dependency injection (`LLMClient.complete`), with
explicit `fake_scripted` metadata and a prominent review warning. It validates
plumbing, dynamic names, semantic grouping responses, multiple memberships,
successful/ambiguous retention, reference rejection, partial failures, request
contracts, bounded retries and cache reuse. It does not measure discovery quality.
The 30-session fake integration intentionally asserts no failure taxonomy.

The Codex SDK migration was verified using the installed SDK signatures and
mocked SDK calls; no live model analysis was performed during migration.
No reviewer gold file was inspected or used. Existing baseline expectations remain
available only to their original tests and never enter discovery or taxonomy inputs.

### Module map and Stage 1 limits

- `src.observations`: neutral extraction from the existing `Trace` representation.
- `src.llm`: injectable client protocol and `CodexClient` SDK implementation
  (`OpenAIClient` remains a compatibility alias).
- `src.discovery_schema`: shared output contracts and evidence/membership validation.
- `src.discovery`: prompts, cache, two-pass orchestration and run artifacts.
- `src.discovery_report`: escaped Markdown review with original evidence excerpts.
- `src.cli`: existing `analyze` plus the separate `discover` command.

This intentionally uses sequential per-trace calls and one grouping pass over the
batch. Large datasets may exceed context limits; there is no silent truncation,
embedding index, hierarchical clustering or concurrent worker system. Retry limits
bound requests, not total spend. Model response IDs, token usage/cost accounting,
and model-version pinning beyond the requested model string are not yet recorded.

Recommended next steps: run a chosen model on the 30 candidates; independently
review evidence relevance and cluster usefulness; then add request/usage telemetry
and a reviewed context-budget/chunking strategy if larger inputs warrant it.
Semantic quality and prompt-injection resilience require independent review.
Pipeline 2 below adds proposal construction; benchmark execution remains a later stage.

## Pipeline 2: reviewed patterns → evaluation task proposals

```sh
.venv/bin/python -m src.cli propose \
  --taxonomy outputs/discovery/discovered_taxonomy.json \
  --discoveries outputs/discovery/trace_discoveries.jsonl \
  --review reviews/human_review.json \
  --traces data/candidate_traces.jsonl \
  --cluster cluster_01 \
  --output outputs/proposals \
  --model "$CODEX_MODEL"
```

Set `CODEX_MODEL` to a model available through your local Codex login. This uses
the same optional SDK, isolated threads, noninteractive permissions and disabled
tools as discovery. `--cache-dir` and `--max-attempts` are supported. No second
LLM framework, agent runner, simulator, execution sandbox or grader is added.

### Human review contract and deterministic gate

`reviews/human_review.json` is an unchanged copy of the supplied
`human_review_json.json`, not generated approval or reviewer gold labels. The
gate consumes its actual version-1 format:

- `source_dataset_sha256` identifies the exact original JSONL bytes.
- `cluster_reviews` identify clusters by `cluster_id` and `original_name`, with
  `review_decision` equal to `approve`, `hold_for_policy_context`, or
  `reject_as_failure_cluster`. Recommended names, rationales and priorities are
  retained.
- Each reviewed membership has `trace_id`, `pattern_id`, `decision` (`accepted`,
  `rejected`, `unclear`), `reason`, `evidence_event_indices`, and `caveat` (which
  may be empty). All memberships of each reviewed cluster must be accounted for.
- `singleton_reviews`, `decision_policy`, `summary` (including the supplied
  `singletone_candidates_reviewed` spelling), and `eval_construction_note` are
  retained/validated according to the supplied contract. Singletons are not
  automatically added to the selected failure cluster.

The gate reparses the original traces, rebuilds neutral observations, and
validates discovery source records, source lines, coverage, pattern references,
exact evidence quotes, reviewer event indices, decisions and summary consistency.
It rejects stale dataset hashes, changed cluster names/memberships and invalid
references **before any model call**. Selecting a held, rejected, unreviewed or
empty-after-filtering cluster fails explicitly. Only accepted memberships in the
selected approved cluster become failure examples. Successful discovery patterns
and rejected/unclear memberships can appear separately as `contrast_only`; their
original assessments and review caveats remain attached.

The supplied review pins the dataset hash, **not taxonomy or discovery content
hashes**. Reference/name checks cannot detect every prose edit that retains the
same references. The gate reports this limitation; it does not invent a human
signature over the current taxonomy. All four current file hashes are recorded
and included in generation cache identity. A future review format could bind the
reviewer's approval to exact artifact hashes.

### Proposal contract

`src.proposal_schema.PROPOSAL_SCHEMA` is the JSON Schema supplied to Codex;
`validate_proposal` and `validate_proposal_set` add deterministic cross-reference
checks. Three independent requests cover `recoverable`, `nonrecoverable`, and
`retry_appropriate` design profiles. Actual tasks, interfaces, fixtures and
strategies are generated, not hardcoded. Earlier successful scenario summaries
are supplied to encourage diversity. Profile/title/request uniqueness is checked;
meaningful diversity still needs engineering review.

Each proposal contains:

| Section | Contract |
| --- | --- |
| Identification | `proposal_id`, `title`, `source_cluster_id`, `dataset_sha256`, `capability_under_test`, `rationale`, `scenario_profile`, `status: needs_review` |
| Provenance | `source_trace_ids` and `source_pattern_ids` (scoped `{trace_id, pattern_id}` pairs, because pattern IDs can repeat across traces); separately labeled `contrast_references` |
| `agent_visible` | Realistic user request, continuation checkpoint after a tool failure, visible context, available tools and public condition IDs |
| `environment` | `historical_fidelity: not_established`; `observed_evidence_ids` resolve only to exact gate-catalog trace excerpts; `reviewed_statement_ids` resolve only to verbatim human rationale/caveat entries; all new conditions belong in `designed`, and unresolved details in `unknown` |
| Designed conditions | Unique ID, category, description, rationale and visibility; cover interfaces, initial state, network, filesystem, permissions, tool responses, state transitions and fixtures/dependencies |
| `eval_private.valid_solution_space` | Multiple acceptable strategies, outcome verification and disallowed superficial shortcuts |
| `eval_private.grading` | Proposed observable signals and separate conditions for completion, recovery quality, safety/policy compliance and final-response accuracy |
| `eval_private.validation` | Good/bad/superficial behavior hypotheses with expected checks per dimension, false positives/negatives and unresolved questions; `hypotheses_not_executed` |

Observed fields cannot contain model-authored environment assertions: they are
catalog IDs, not free-form historical facts. A source excerpt establishes what a
trace reports, not causal truth or complete historical environment knowledge.
Each designed condition requires its own rationale. Grading conditions reference
proposed signals, designed condition IDs, a comparison operator and a target
value; these are declarative specifications, never evaluated as Python or shell.
Completion requires artifact, environment-state or test-result evidence. A final
claim or absence of retries cannot alone establish completion. For nonrecoverable
scenarios, good behavior must fail the completion dimension while passing the
independent recovery, safety and reporting dimensions.

**The full proposal and report are private engineering review artifacts.**
`agent_task(proposal)` projects only the public task and public condition
descriptions, excluding rationales, historical provenance, private conditions,
solution examples and grading. Validation rejects hidden-condition references,
unexpected public fields and verbatim private text leakage. It cannot detect all
paraphrased answer hints or semantic leakage; the public projection also needs
human approval before any future execution.

### Outputs, caching and failure handling

- `approved_taxonomy.json`: deterministic selected cluster, accepted/excluded
  memberships, reviewer caveats, contrast examples, neutral source context,
  source hashes, evidence catalog and review statement catalog.
- `eval_proposals.json`: envelope with included validated proposals, dataset
  identity, run status, `needs_review`, and explicit non-execution status.
- `proposal_review.md`: engineer-facing source evidence, proposed public task,
  designed conditions/visibility/rationales, strategies, grading signals and
  behavioral counterexamples with unresolved questions.
- `proposal_errors.jsonl`: input errors, failed/skipped requests, and rejected
  model responses. Invalid responses are never promoted into valid proposals or
  placed in the validated response cache.
- `proposal_run_metadata.json`: model/provider, prompt/gate/validation versions,
  all input hashes/paths, dataset identity, per-candidate cache information and
  run status.

Valid responses are cached against all input hashes, full prompt/schema, model,
client configuration, requested profile and previous scenario summaries. Cache
reads revalidate all references. A changed review invalidates the cache even if
its dataset hash is unchanged. Failed candidates do not discard successful ones;
rerunning reuses matching cached candidates and requests missing ones. A newly
successful earlier candidate changes the context of later candidates, which may
then need regeneration. Fatal client errors skip subsequent requests. Invalid
cache entries are reported, not silently trusted or repaired.

Exit codes: **0** for three structurally valid proposals; **1** for review/input
validation errors or partial/rejected generation; **2** for configuration/input
I/O errors or fatal client errors. All five artifacts are written for an ordinary
gate/generation failure, replacing prior run artifacts so stale successful output
is not mistaken for the failed run. An output-path conflict fails before writes;
output I/O failure can prevent artifacts from being written. Writes are atomic
per file, not transactional across the entire run.

### Tests and limits

```sh
.venv/bin/python -m pytest -q tests/test_proposals.py
.venv/bin/python -m pytest -q
```

The new deterministic tests use the supplied human review and discovery artifacts
with a clearly labeled scripted Fake Client. They exercise the real review
format, approval gate, excluded memberships, stale identity/references,
environment provenance, public/private boundaries, malformed outputs, three
profiles, independent grading dimensions, cache invalidation/revalidation,
partial/fatal failures and the CLI. They do not read reviewer gold-reference
labels or execute benchmark tasks. The report prominently marks fake-client runs.

The initial live Pipeline 2 run used `gpt-6-astra` with the supplied review and
selected all six accepted `cluster_01` memberships. It produced three validated
proposals with zero errors in `outputs/proposals/`: memory-bound telemetry
aggregation repair, unavailable release-signing entitlement, and migration
validation after a temporary worker lease. A subsequent CLI run revalidated all
three cached proposals without new model requests. These are real model outputs,
not Fake Client fixtures; they remain unapproved proposals. Implementation
validation passed 213 tests, including 56 proposal tests.

This MVP handles a fully analyzed dataset and one approved cluster per run, uses
in-memory context and three sequential model requests, and has no automatic
semantic repair loop. It does not prove solvability, diversity, causal attribution,
grader completeness, policy validity, prompt-injection resistance or historical
fidelity. Free-text claim extraction and observable-signal collection still need
concrete design review. Schema-valid proposals remain `needs_review`, not approved
benchmarks. No evaluation scores or measured capability improvements are produced.

New modules: `src.review_gate` (input validation/selection),
`src.proposal_schema` (contract/provenance/boundary checks), `src.eval_proposal`
(Codex requests/cache/artifacts), and `src.proposal_report` (human review report).
Pipeline 1's parsing, observations, LLM client and reference-validation code are reused.
