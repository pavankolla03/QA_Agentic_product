# Architecture

Design notes for people extending the platform. The README covers what it does; this covers *why
it is built this way*.

---

## 1. The central split: reasoning vs acting

Every design decision follows from one rule:

> **The LLM decides what should happen. Deterministic code decides what is allowed to happen.**

An agent may conclude "I should delete `src/index.ts`". The `WorkspaceGuard` refuses, the refusal is
audited, and the agent gets a `ToolResult` explaining why. Nothing about the model's confidence,
phrasing, or a prompt-injected instruction inside a test fixture can change that outcome.

Concretely:

| Concern | Owned by |
|---|---|
| What to test, how to phrase Gherkin, how to name a page object | LLM |
| Whether a file may be written, a command run, a branch committed | `packages/security` |
| Which coding standards apply and whether they pass | `agents/standards` rule engine |
| Whether a failure is healable | Deterministic signatures first, LLM only for the ambiguous remainder |
| Test ids, tag completeness, coverage gaps | Post-processing in `agents/test_design` |
| Numbers in the report | Computed in `agents/reporting`; only the prose is model-written |

When a model and a deterministic check disagree, the deterministic check wins.

---

## 2. Layer boundaries

```
apps/          ← clients (VS Code, dashboard). No QA logic.
services/      ← orchestration, persistence, routing, HTTP
agents/        ← reasoning units. One responsibility each.
tools/         ← side effects. Every one policy-guarded and traced.
packages/      ← leaf libraries. No imports from services/ or agents/.
configs/       ← policy as data
```

The dependency graph is acyclic and enforced by the import structure. One subtlety worth knowing:
`packages/agent_protocol` exists purely so `services/observability` can tell "this agent is waiting
for a human" apart from "this agent crashed" **without importing the agent layer** (which imports
observability). Control-flow signals therefore live in a leaf package.

---

## 3. The orchestrator is a resumable state graph

Shaped like LangGraph — named nodes, conditional edges, an interrupt that suspends — but implemented
directly (`agents/orchestrator/graph.py`), for one reason that matters more than the dependency
saving: **a suspended run must be durable.**

A generator-based graph holds its state in Python. If a QA engineer starts a run at 5pm, goes home,
and approves the diff the next morning, the process holding that generator is long gone. So the
graph state lives in the `runs` row instead: artifacts as JSON documents, granted approvals and the
suspended node in `metadata_json`.

Resume is therefore just:

```python
snapshot = load_run(run_id)                 # from Postgres/SQLite
ctx      = rehydrate(snapshot)              # requirement, plan, bundle, …
result   = await orchestrator.run(ctx, start_at=snapshot["suspended_at"])
```

`tests/integration/test_engine_and_api.py::test_a_suspended_run_survives_a_fresh_engine` builds a
brand-new `AgentEngine` between the pause and the approval to prove there is no in-memory state.

### Control flow

```
requirement → repository → exploration → test_design ──[approve]──┐
                                                                  ▼
   reporting ◄── commit ◄── execution ◄── standards ◄── code_generation
                    ▲            │
                    │            ├─ failures? → failure_analysis
                    │                              │
                    └──────── self_healing ◄───────┘   (bounded loop)
```

The heal loop terminates on three conditions, checked in `_after_self_healing`: no failures left,
the iteration ceiling, or *no repair was verified in this pass* — the last one prevents looping while
producing the same rejected proposals.

---

## 4. Why generated code is trustworthy

Three mechanisms, in order of importance.

**Repository grounding.** Before generating anything, the Repository Agent extracts the real layout,
the real base class, the real fixtures and the real naming conventions (`services/knowledge_service`).
This is static analysis, not a prompt — regex extraction that degrades to "found nothing" rather
than hallucinating. The resulting briefing goes into every generation prompt, and reuse claims are
cross-checked: if the plan says it will reuse `ResidentPage` and no such symbol exists, the platform
corrects the plan rather than trusting it.

**Verified locators.** The Exploration Agent drives a real browser and harvests a locator catalogue
ranked by the organization's strategy priority (`getByTestId` → `getByRole` → … → xpath). Generated
`getByTestId` calls are then cross-checked against that catalogue; anything not observed in the live
DOM is annotated with `TODO(aiqa)` rather than left looking authoritative. When the application is
unreachable, the agent says so and the report tells the human to verify locators — it does not
quietly invent plausible selectors.

**Deterministic rendering where possible.** Feature files are rendered from the approved `TestPlan`
object, not re-generated by a second model call. The Gherkin a human approved is byte-for-byte the
Gherkin that lands.

Every artifact also has a deterministic template fallback, so a model outage degrades output quality
rather than breaking the pipeline.

---

## 5. Failure triage and the safety override

The highest-stakes judgement in the system: **broken test, or broken product?** Getting it wrong
towards "test" means auto-repairing a genuine defect until the suite stops detecting it.

Three layers:

1. **Deterministic signatures** (11 patterns) handle unambiguous cases — `resolved to 0 elements`,
   `ECONNREFUSED`, `duplicate key value`. Above 0.75 confidence these are authoritative and the model
   is not consulted for the category at all.
2. **The model** handles the ambiguous remainder, given the error, the stack, the failing step and
   the live DOM catalogue. It only overrules a signature if it is *more* confident.
3. **A safety override runs last.** If the failure text shows a value-level assertion mismatch
   (`Expected: … Received: …`, `toHaveText`, `toEqual`), the analysis is forced to
   `is_product_defect` and `NO_ACTION` regardless of what the model said.

The asymmetry is deliberate: a false "product defect" costs a human five minutes; a false "just fix
the test" can ship a bug.

`UPDATE_EXPECTED_VALUE` is never applied automatically — rewriting an expectation to match observed
behaviour is precisely how a suite silently stops testing anything.

---

## 6. Self-healing is verify-or-revert

A repair is only allowed to be:

* **Scoped** — healable categories only, never a suspected defect.
* **Minimal** — a snippet replacement, so the human reviews two lines, not two hundred.
* **Evidence-based** — a relocated selector must come from the live DOM catalogue, and a clear
  winner is required. Two similar candidates means the agent declines rather than guesses.
* **Verified** — the repaired test is re-run; if it still fails the change is reverted.

Four hard rejections apply to every proposal, model-generated or not
(`agents/self_healing/agent.py`): it must not reduce the assertion count, must not comment out or
skip a test, must not introduce a hard wait, and must not reference a test-id that was never
observed. Every proposal — applied, verified or reverted — is recorded in `heal_history`.

---

## 7. Observability

Mandatory traceability, as the spec requires. Every run carries
`run_id / project_id / user_id / repository_id / session_id / timestamp / status`, and each agent
step records provider, model, prompt/completion tokens, cost, latency, status, error and tool calls.

```
runs ─┬─ agent_traces ──┬─ llm_calls      (provider, model, tokens, cost, latency, fallback_from)
      │                 └─ tool_calls     (category, tool, args, result, status)
      ├─ run_events                       (append-only; replays a run after the fact)
      ├─ approvals                        (who decided what, when, with what comment)
      └─ audit_log                        (append-only; every write, command, commit, denial)

cost_daily        pre-aggregated by day × org × project × user × provider × model
heal_history      every repair ever proposed
flaky_tests       per-test flake ledger driving quarantine advice
knowledge_chunks  embedded repository chunks for retrieval
```

Cost is computed at the point of the call from the price table in `configs/models.yaml`, so a model
swap re-prices automatically. Free and local models are genuinely zero, not estimated.

`RunTracker` is fail-soft throughout: every persistence path is wrapped so a tracing error can never
fail a QA run.

---

## 8. Model routing

Agents request a *capability* (`fast`, `reasoning`, `coding`, `embedding`), never a model. The router
walks the candidate list for that capability, skips unconfigured and unhealthy providers (health is
cached for 60s), retries retryable errors with backoff, and falls back down the chain — recording
`fallback_from` so the trace shows what happened.

Two terminal fallbacks guarantee the platform always works:

* **`mock`** — a deterministic offline engine. Not just a test double: it produces structurally valid,
  contextually relevant artifacts from prompt heuristics, so the platform is evaluable with zero
  credentials.
* **`hashing`** — a local hashing embedder (unigrams + bigrams, 512 dims). Less semantically rich
  than a neural embedder, but instant, free and dependency-free, so repository retrieval works on a
  laptop with nothing installed.

Retrieval is hybrid — 0.65 × cosine + 0.35 × lexical overlap. The lexical component rescues exact
identifier lookups that embeddings routinely miss, which matters a great deal when the query is a
class name.

---

## 9. Human-in-the-loop

Gates are policy, not code: `configs/standards.yaml` lists which actions require approval, and
`auto_approve_below_risk` sets the threshold below which low-risk actions proceed automatically.

Default gates: `test_plan`, `code_write`, `git_commit`, `git_push`, `self_heal_apply`,
`destructive_command`.

An agent raises `ApprovalRequired`; the orchestrator persists the request and returns. The gate
carries a real unified diff, so the reviewer sees exactly what will land — in the VS Code diff
editor, or rendered in the dashboard. A rejection stops the run and records the reason in the audit
log.

`auto_approve` exists for CI and requires the `approval:respond` permission — an engineer cannot
escalate their own autonomy through the API.

---

## 10. Extending it

**A new agent:** subclass `BaseAgent`, implement `run(ctx)`, register a `Node` in
`build_default_nodes()`. Use `self.ask_json(...)` for structured output — it retries once on
malformed JSON and falls back deterministically, so a downstream agent always receives something
valid.

**A new tool:** subclass `Tool`, implement `_run(**kwargs) -> ToolResult`, add it to
`ALL_TOOL_CLASSES`. Tracing, redaction and error containment come from the base class; raise
`PolicyViolation` to refuse an action and it will be audited automatically.

**A new standards rule:** add it to `configs/standards.yaml`. Regex rules need no code. A semantic
rule needs a function in `SEMANTIC_CHECKS` keyed by its `check:` name.

**A new LLM provider:** subclass `BaseProvider`, implement `_chat`, register it in
`ModelRouter.provider()`, add it to `configs/models.yaml`. Redaction and usage normalisation are
handled by the base class.

**A new framework target (Cypress, WebdriverIO, pytest):** `detect_framework` already recognises
them. The work is in `agents/code_generation` templates and the `layout`/`naming` sections of the
standards file — the rest of the pipeline is framework-agnostic.

---

## 11. Known trade-offs

* **Regex-based code parsing** instead of a real TypeScript AST. Shipping a Node toolchain just to
  *read* a repository was not worth it; the parser degrades to "found nothing" on unusual syntax
  rather than crashing, and everything downstream tolerates a sparse profile.
* **Full artifacts stored as JSON on the run row.** Denormalised and occasionally large, but it makes
  a run perfectly replayable and resumable without a second store.
* **Health checks cached for 60 seconds.** A provider that dies mid-run is not noticed immediately;
  the fallback chain absorbs it on the next call.
* **The offline engine is heuristic.** It guarantees a working pipeline, not good test design. The
  dashboard and extension both say so prominently rather than letting a user mistake it for the real
  thing.
