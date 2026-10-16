# Real-model Critic regression review

Provider: `openai_codex`; model: `gpt-6-astra`; Critic: `validity-critic-1`.
The three real model responses were preserved unchanged and revalidated from cache after fixing the sidecar validator's handling of empty, inapplicable feedback fields. This is a real-model result, not a Fake Client test. Expected issue matching below was performed after seeing the outputs; these expected findings were not sent to the production Critic.

## A — Explicit recovery hint: detected

Fixture: `explicit_hint.json`; finding `CV-01` (major).
The Critic states that an agent can copy the prescribed sequence without diagnosing the blocker. It also identifies an additional criterion problem (`CR-01`): executing the valid repair directly fails recovery, whereas reading an uninformative README earns it. Its robustness finding (`RB-01`) correctly distinguishes removing the only recovery information from merely rewording it.

## B — Equivalent wait strategies: detected

Fixture: `equivalent_wait.json`; finding `CR-01` (major).
The Critic explicitly compares `wait(2) → check.run(unit)` with `wait(1) → wait(1) → check.run(unit)`. Both reach the same goal lawfully, but the fixed progress-count criterion passes the first and fails the second. The offline checker independently reproduces the score difference. The Critic also notes missing diagnostic information, hidden permission boundaries and retries ignored by scoring.

## C — Superficial diagnostic action: detected

Fixture: `irrelevant_diagnostic.json`; finding `CR-01` (critical).
The Critic observes that reading the office-picnic announcement earns recovery credit, even if followed by unchanged retries, while a direct productive configuration-and-check sequence fails recovery. This identifies both a false positive and a false negative. The observation's irrelevance is a semantic finding; an event-count witness alone would not establish it.

## Interpretation and limits

All three intended concerns appeared in this small held-out run. This does not estimate general detection accuracy or prove eval validity. The Critic saw proposals, the reviewed cluster/evidence context and offline contract probes; this is the integrated Critic, not a blind text-only baseline. Its additional findings are hypotheses for human review, not new approved defects. Fixture designs deliberately include flaws and should not be executed as approved evaluations.

All three outputs also flag that the v2 final-response grader checks only the structured completion boolean and cannot detect fabricated narrative summary content. That limitation is real and intentionally remains unchanged in Pipeline 3. Humans must decide whether the metric's interpretation is appropriately scoped; no prose-semantic grader was added.

The synthetic fixtures contain underspecified public task details and some inconsistent behavior hypotheses, which the Critic also criticizes. Their findings must not be generalized to the historical approved v2 specifications without examining those specifications. Real Agent performance and robustness variants have not been executed here.

See `critic_regression_results.json` for complete findings, proposed variants, behavior examples, cache identity, provider, model and errors. Fake Client tests independently verify workflow and schema contracts only.
