# The pipeline

Two planes, separated because they want opposite things — reached from anywhere.

```
  VS Code   Slack   Teams   WhatsApp   voice   CI   API
     └────────┴───────┴─────────┴────────┴──────┴─────┘
                            │
                   ┌────────┴────────┐
                   │     gateway     │   one CommandEnvelope in,
                   └────────┬────────┘   one Reply out
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

## Channels

An adapter does two things: turn what arrived into a `CommandEnvelope`, and turn
the `Reply` back into what that channel renders. It never decides what a message
means.

That line matters more than it looks. Six channels with six approximations of
"is this a greeting or a request to automate an application?" agree in week one
and disagree by week four, and the disagreements get found by whoever's run did
not start.

| channel | arrives as | reply shaped as |
|---|---|---|
| VS Code | the chat panel's own SSE stream | streamed tokens |
| Slack | Events API JSON, or a form-encoded slash command | `blocks` plus a text fallback |
| Teams | a Bot Framework activity | a message activity |
| WhatsApp | Cloud API nesting, or a Twilio form | one text body |
| voice | a transcript and a call id | one spoken sentence |
| CI | an instruction, a commit, a branch | `{ok, run_id, error}` |
| API | the envelope's own fields | the reply's own fields |

Three rules the gateway does not bend:

* **Sessions are keyed by channel *and* conversation.** A Slack thread id and a
  WhatsApp chat id can be the same string, and a collision would hand one person
  another person's project. Two threads about two services are two contexts.
* **An unbound conversation is asked, not defaulted.** With several projects
  registered and none named, picking the most recent would write files into a
  repository nobody mentioned, and the person would find out from the diff. With
  exactly one project the question is only friction, so it is not asked.
* **Webhooks authenticate like everything else.** A Slack body names a Slack
  user; it does not say whether that person may start a run, and the payload is
  precisely where an attacker has full control.

Only voice is rewritten for its channel, and only structurally: a phone call has
no scrollback, so a run id or a file path read aloud is noise nobody can
re-read. Typed channels get the text as written — truncating prose to fit a
notification cuts off the important half.

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

## What a finished run calls itself

`status` is the one word everything downstream reads: the dashboard colours it,
CI gates on it, the chat answers "did it work?" from it. None of them open the
report, so it has to be the honest word.

| status | means |
|---|---|
| `succeeded` | there is automation here that was verified |
| `blocked` | the platform worked and produced nothing it can vouch for |
| `failed` | the platform itself broke |

A suite that ran and reported genuine failures is `succeeded`. Going red for a
real defect is the job, not a malfunction.

A run is `blocked` when execution could not run, when steps have nothing behind
them, when the code does not compile, or when the runner exited cleanly having
executed no test at all — that last one nothing else catches, because there are
no failures to count.

The rule used to be "succeeded if a report was written, else failed", which made
producing a report the definition of success. A real run against the demo
application was recorded as `succeeded` with four compile errors, eight unbound
steps, zero tests executed, and a report headlined AUTOMATION_BLOCKED. Writing a
document about not finishing is not finishing.

---

## Automating from a URL alone

Give the platform a URL and nothing else, and the application becomes the
specification.

```
"https://shop.example.com"
          │
          ▼
  requirement          writes NO acceptance criteria  ← the whole design
          │                                              rests on this
          ▼
  exploration          crawls up to 30 pages, then names what it can see
          │
          ▼
  test_design          refuses outright if exploration found nothing
```

### Signing in first

Everything worth testing is behind a login. Without credentials the crawler
follows every navigation link, is redirected back to the sign-in page each time,
and records one screen several times over — which looks like a small application
rather than like a failure, so nothing downstream questions it.

```
"https://app.example.com user: qa.bot pass: ..."
          │
          ├─ credentials stripped from the text before it becomes the
          │  instruction, stored in the project's .aiqa/ (git-ignored),
          │  never in the database and never in a prompt
          ▼
   sign in ──── verified? ──no──▶ run BLOCKED, naming the failure
          │
         yes
          ▼
   crawl everything behind the login, never following "Log out"
```

Three refusals worth naming. The sign-in is **verified**, not assumed —
submitting a form is not the same as holding a session. The sign-in page is
**captured before leaving it**, or the one screen every user meets has no
coverage for the same reason it has no page. And a link that ends the session is
**never followed**, because every page after it would be the login screen again.

Credentials reach the generated suite as `AIQA_APP_USERNAME` and
`AIQA_APP_PASSWORD`, read at run time, so the output can be committed and run in
CI without a password entering the repository.

### Where the scenarios come from

The requirement stage produces an empty requirement on purpose. A model asked
"what should I test at this URL?" answers confidently and completely wrongly:
forty plausible scenarios for a site it has never loaded, each bound to a
locator that does not exist, and every later stage treats that as ground truth.

So the criteria come from the crawl, and only from what the crawl is evidence
for:

| seen | claimed |
|---|---|
| a form containing a password field | a sign-in, with a wrong-password path |
| a field marked required | a rejection path for that field |
| rows counted on the page | "shows at least one row" |
| no rows | nothing — rather than a test that fails on clean data |
| a validation message | *evidence in the rationale*, never an assertion |

That last row is the discipline in miniature. The message was seen; what
triggers it was not, and a "then" without its "when" is an invention.

When the crawl finds nothing, nothing is written and `test_design` fails naming
the URL. A failed run naming the URL is the honest outcome; a green run full of
fiction is not.

### No model between the crawl and the code

For autopilot the scenarios and the page objects are both rendered from the
discovered features rather than designed by a model. Asking one to turn observed
features into Gherkin produced `Then the resident should exist in the database`
for an application with no database step, `Then the reports page should load`
which asserts nothing, and a `Scenario Outline` whose placeholder was `{string}`
with no Examples table. Asking one to turn those scenarios into page objects
produced classes with no `goto`, no way to sign in and nothing that could say
"we are still on this page" — fifteen good scenarios, twenty blocked steps.

Both ends are known: the vocabulary is fixed, the elements were observed. When
both ends are known the mapping is a lookup, not a judgement.

**Only real data is quoted.** Cucumber turns a quoted fragment into a `{string}`
parameter, so `I fill in "Full name" with "QA Autopilot"` compiles down to `I
fill in {string} with {string}` — one step matching every field and bindable to
no particular element. The page and the field belong in the sentence; the value
somebody types is the only part that varies. For the same reason a field is
"left empty" by *not filling it* rather than by a step that names it.

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
