# Cost optimization

The design objective: **minimize LLM usage without sacrificing automation quality.**

A naive implementation of this product re-sends the repository, re-crawls the
application and re-designs the same scenarios on every request. That is where
essentially all the money goes, and it buys nothing — the repository has usually
not changed, the application has usually not changed, and half the scenarios
already exist.

Everything below exists to avoid paying for the same knowledge twice.

---

## 1. The rule that comes first

> **The LLM reasons. Deterministic code acts.**

| Never uses an LLM | Uses an LLM |
|---|---|
| Reading, writing, searching files | Requirement analysis |
| Parsing JSON / XML / JUnit / Gherkin | Test architecture and design |
| Running Playwright, npm, `tsc`, ESLint | Code generation |
| Git status, diff, branch, commit | Failure root-cause analysis |
| Cost arithmetic, tracing, notifications | Self-healing decisions |
| Collecting screenshots, videos, traces | The narrative paragraph of a report |

Asking a model "is this valid TypeScript?" is the single most wasteful call the
platform could make. `tsc` answers it exactly, instantly, for free.

---

## 2. Four tiers, not one model

Agents request a **tier**; `configs/models.yaml` maps it to a concrete model.

| Tier | Used for | Default |
|---|---|---|
| `reasoning` | requirement analysis, test architecture, root-cause, healing decisions | GLM 4.6 → Claude → DeepSeek R1 (free) → local |
| `coding` | TypeScript, Playwright, Page Objects, steps | Qwen 2.5 Coder 32B → free tier → local |
| `cheap` | summarization, classification, metadata, report prose | Llama 3.3 70B (free) → local → GLM |
| `embedding` | repository/application retrieval | nomic-embed (local) → OpenAI → local hashing |

Three mechanisms keep work on the cheapest tier that can do it:

**Task overrides.** One agent can mix tiers. `failure_analysis.classify` is
`reasoning`; `failure_analysis.triage_bulk` is `cheap`.

**Deterministic complexity scoring.** `score_complexity()` rates a task from
prompt size, artifact count, retry count and hard-problem signals. It costs
nothing, so it can gate every call. Trivial work on the `reasoning` tier is
*downgraded*; genuinely hard work on `cheap` may *escalate* — bounded to three
escalations per run, each recorded in the trace.

**Request accounting.** OpenRouter's free tier is request-limited, not
token-limited. The router tracks daily consumption and stops routing free work
there once it hits the reserve, so a long day does not fail mid-run.

---

## 3. Never re-derive what you already know

### Repository Map — `.aiqa/repository_map.json`

Content-hashed per file.

```
git HEAD unchanged and working tree clean?
        └── yes → return the cached map. Zero file reads. Zero embeddings.
        └── no  → hash every candidate file, diff against the map
                   ├── unchanged → reuse the parse result verbatim
                   ├── modified  → re-parse and re-embed only this file
                   └── removed   → drop its chunks
```

Measured on the bundled fixture: a cold pass indexes 10 files and embeds 10
chunks; a warm pass with one file touched re-embeds **one**.

A subtle bug worth knowing about: the map writes itself *into* the repository,
which makes `git status` dirty. Left unhandled, that defeats the cache forever.
Dirtiness is therefore filtered to files that would actually change the index —
`.aiqa/`, `node_modules/`, lockfiles and non-indexable extensions are ignored.

### Application Map — `.aiqa/application_map.json`

Designing 500 scenarios against one application should explore it **once**.

Every locator carries a confidence, a source and a `last_verified` timestamp. A
route is re-crawled only for a nameable reason:

- never seen
- DOM hash changed
- a locator from it failed in a recent run
- more than half its locators have decayed below the trust floor
- the entry is older than the TTL (7 days)
- the application version changed
- it was captured by the HTTP fallback **and** a browser is now available

Confidence is a feedback loop: repeated success approaches certainty
asymptotically; a single failure multiplies confidence by 0.45. Failure is
punished harder than success is rewarded, because re-exploring is cheap and
trusting a stale selector is not.

### Test Knowledge Store and QA Knowledge Graph

Every scenario the platform produces is remembered with its feature file, page
objects, fixtures, APIs, tables, routes and execution history. Before designing
anything, the Test Design Agent asks what already exists.

Similarity is deterministic — a blend of containment and Jaccard over
significant tokens. Plain Jaccard is the wrong metric: a five-word request
scored against a stored test's full signature comes out near zero even when the
request is entirely covered by it.

The graph links requirement → feature → page → component → API → table → test →
failure, so retrieval returns the relevant *neighbourhood* rather than the
repository.

---

## 4. Static analysis before semantic review

```
generated code
    ├── structure and naming        (instant)
    ├── Gherkin parser              (instant)
    ├── regex + AST standards rules (instant)
    ├── tsc --noEmit                (seconds, exact)
    └── ESLint                      (seconds, project's own rules)
            │
            ├── errors? → back to Code Generation. No model call: there is
            │             nothing for a model to judge about code that does
            │             not compile.
            └── clean?  → one cheap semantic review, for conventions no tool
                          can express
```

Each checker degrades to "skipped, here's why" when its tooling is absent, so a
repository without ESLint still gets everything else.

---

## 5. Design in batches, generate only what is new

One planning call designs every scenario for a feature. Six calls would cost six
times as much and produce a less coherent suite, because no call sees the others.

The prompt carries what already exists — similar tests, reusable page objects,
fixtures, components and the graph neighbourhood — so the model is asked to
design only the gap. Anything it designs anyway that duplicates existing
coverage is dropped deterministically before code generation.

---

## 6. Hard ceilings

Three independent limits, checked before every call and restored across a
resume so a suspended run cannot reset its own budget:

```yaml
run_budget:
  max_requests: 40           # free tiers are request-limited
  max_input_tokens: 400000   # context is what actually costs
  max_output_tokens: 120000
  max_cost_usd: 2.00
  max_retries: 2
  max_correction_attempts: 3
  max_healing_attempts: 2
  max_execution_retries: 3
```

A call the budget cannot fit is refused **before** it is paid for. Free models
are exempt from the cost ceiling but not the request or token ceilings. When a
ceiling trips the run stops and is flagged for human review rather than
continuing on a smaller model and producing worse work silently.

---

## 7. Measuring, not claiming

```bash
python -m scripts.benchmark --runs 5          # offline: requests, tokens, cache hits
python -m scripts.benchmark --runs 5 --live   # with real providers: dollars
aiqa cost report                              # from real usage
```

The benchmark starts a real application (`scripts/demo_app.py`), runs a realistic
workload several times against one repository, and reports the delta between the
cold run and the warm ones.

**What offline mode proves:** request counts, token volume, cache hit rates and
duplicate avoidance. These are the quantities the architecture controls and they
are not affected by which model answers.

**What offline mode does not prove:** dollars. The deterministic provider is
free, and it emits a fixed template regardless of the "do not redesign what
already exists" instruction — so warm-run token counts in offline mode
*understate* the architecture. Use `--live` for a cost figure on your own
provider mix.

Against the stated baseline of ~30,000 credits for 110 tests (≈273/test):
credits and dollars are not comparable across providers, so compare **requests
and tokens per scenario** and run `--live` for money.

---

## 8. Where the savings actually come from

Ranked by impact, from measurement rather than intuition:

1. **Not re-indexing an unchanged repository.** The most common case by far.
2. **Not re-crawling known pages.** Second most common, and the more expensive of
   the two in wall-clock terms.
3. **Not designing scenarios that already exist.** Large when a team iterates on
   one feature area.
4. **Static analysis instead of model review.** Removes a call per generated file.
5. **Tier routing.** Turns most calls free without touching the ones that matter.
6. **Prompt caching.** The standards prefix is stable across runs, so providers
   that support caching bill it once.
7. **Retrieval instead of whole-file context.** Ships the relevant slice, not the
   repository.

---

## 9. Tuning it

| Want | Change |
|---|---|
| Cheaper, slightly lower design quality | Move `test_design.plan` to `cheap` in `task_capability` |
| Better code, higher cost | Put `anthropic/claude-sonnet-5` first in `routes.coding` |
| Fully free | Delete every paid entry; Ollama and free OpenRouter models remain |
| Fully local and private | Set `AIQA_DEFAULT_PROVIDER=ollama` and remove all API keys |
| Stricter spend control | Lower `run_budget.max_cost_usd` and `max_requests` |
| More re-exploration | Lower `DEFAULT_TTL_SECONDS` in `application_map.py` |
| Less re-exploration | Raise the TTL, or raise `CONFIDENCE_FLOOR` |
