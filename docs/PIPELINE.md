# The pipeline

Two planes, separated because they want opposite things.

```
                        a message arrives
                               │
                      ┌────────┴────────┐
                      │  intent lookup  │   deterministic, no model
                      └────────┬────────┘
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
      CONVERSATION PLANE                   TASK PLANE
      answers in milliseconds              runs in minutes
      from the database                    through eleven agents
```

Everything that made the chat feel broken came from running one message through
the machinery built for the other.

---

## Conversation plane

A message is resolved to a named intent before anything else happens. Most
never reach a model at all.

| intent | answered from | measured |
|---|---|---|
| `CONVERSATION`, `HELP` | a fixed string | 180ms |
| `RUN_STATUS` | the run row | 181ms |
| `RUN_FAILURE_QUERY` | the run row | 135ms |
| `COST_QUERY` | the cost table | 196ms |
| `PROJECT_QUERY` | the project row | 164ms |
| `CONFIGURATION_QUERY` | `models.yaml` | 182ms |
| `UNKNOWN` | one model call | ~4s |

"How many tests failed?" is a database row. A model is slower, spends a
request, and can be wrong — the single question type where SQL strictly beats
an LLM, and it was going to a model.

Three properties hold regardless of intent:

* **The policy is not the automation policy.** `interactive_chat` is its own
  tier: five-second ceiling, three hundred output tokens, no retries. The
  automation tiers keep their generous settings, which is what makes them work.
* **One router for the process.** Provider health, rate-limit state, model
  cooldowns and HTTP connections survive between messages. Building a router per
  message threw all of it away and re-probed provider health — up to eight
  seconds — before the model was asked anything.
* **Ambiguity answers, never runs.** A wrong sentence costs a sentence. A wrong
  run costs five minutes and writes files into somebody's repository.

---

## Task plane

```
requirement → repository → exploration → test_design → code_generation
    → step_coverage → standards → execution → failure_analysis
    → self_healing → commit → reporting
```

Eleven stages. The one that is new is `step_coverage`, and it exists because
everything before it could be satisfied by a step that does nothing.

### Where each stage refuses to lie

| stage | what it will not claim |
|---|---|
| `exploration` | that it crawled a page when the browser never launched, or that a 404 is a page |
| `code_generation` | that a locator exists when the crawl never saw it |
| `step_coverage` | that a step is finished when its body is `return 'pending'` |
| `standards` | that it compile-checked anything (it runs before the files exist, and says so) |
| `execution` | that tests passed when the runner never started |
| `self_healing` | that a repair worked when it could not be re-run |

Every one of those is a defect that shipped, was found by running the thing,
and now has a test.

---

## Step coverage

The stage sits between generation and standards. Between, because a step that
does nothing is not a style problem and standards has no way to see it.

```
code_generation
      │
      ▼
 find the gaps ──── none ────▶ standards
      │
      │ a `return 'pending'` body
      │ a call to a method the page never declared
      ▼
 ┌──────────────── the ladder ────────────────┐
 │ 1. an existing step definition             │  nothing to generate
 │ 2. an existing Page Object method          │  generate the binding
 │ 3. a locator the crawler actually observed │  generate method + binding
 │ 4. nothing                                 │  AUTOMATION_BLOCKED
 └────────────────────────────────────────────┘
      │
      ▼
 re-check, at most three passes
```

There is no rung that invents an interaction. That is how a suite ends up
clicking a button which does not exist, and the fourth rung — declining — is
the only useful answer when the application never showed the platform the
control in question.

A block names the step and gives a reason somebody can act on in seconds:

```
AUTOMATION_BLOCKED: 2 step(s) need a human decision.

  "the accountant reconciles the quarterly ledger"
      the step does not name an action I can recognise

  "I enter the resident email address"
      "Email" needs a value and the step supplies none —
      quote one in the step, and I will bind it
```

The loop stops the moment a pass closes nothing. Every pass sees the same
evidence, so a pass that achieves nothing will not achieve anything next time;
the three passes exist for the case where closing one gap reveals the method
another needs.

### The completion gate

A run does not report finished work while any step is blocked. The report leads
with the block, because it used to lead with the count of files written — which
is how a run with three unbound steps read as a success.

---

## Run modes

| mode | stages |
|---|---|
| `plan_only` | through `test_design`, then `reporting` |
| `generate` | through `step_coverage` and `standards`, files written, nothing executed |
| `full` | everything |
| `execute_only` | `repository` → `execution` → analysis → healing → `reporting` |
| `heal_only` | as above, healing only |

`step_coverage` skips itself in `execute_only` and `heal_only`: nothing was
generated, so there is nothing to check.
