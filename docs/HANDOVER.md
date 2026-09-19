# QAgentic — what this is, what works, what does not

Written for whoever picks this up next, human or model, with no access to the
conversations that built it. It is deliberately specific about the parts that
are unfinished, because the failure mode this project cares most about is a
system that reports success for work it did not do — and a handover document
that does the same thing is the same bug one level up.

Every number here was measured on 2026-09-19, not estimated.

---

## 1. What the product does

You give it a URL and an account. It signs in, crawls the application, works out
what is testable, writes Playwright + Cucumber automation for it, runs that
automation, and reports what happened.

```
"http://localhost:8123 user: std.user pass: Passw0rd!"
        │
        ▼
  sign in ──▶ crawl ──▶ name the features ──▶ write scenarios
                                                    │
                                                    ▼
                          page objects ──▶ run ──▶ report
```

You can also describe a feature in prose ("automate the registration form,
including required-field validation") and it will use a language model to read
that. The URL path deliberately does not.

It is reachable from VS Code, Slack, Teams, WhatsApp, a voice transcript, CI, or
a plain HTTP call. All of them go through one gateway so a message means the
same thing wherever it arrives.

---

## 2. The one idea that explains most of the code

**Never report success for work that did not happen.**

Almost every non-obvious decision in this repository follows from that. It is
not a slogan — it is the recurring bug. Found and fixed in, among others:

| Where | What it claimed | What was true |
|---|---|---|
| compile gate | passed | `npx` never started, no output, read as success |
| exploration | crawled the app | browser never launched; 0 pages |
| crawler wrapper | `simulated: false` | the script had said `simulated: true` |
| step coverage | gap closed | bound to a method on a different page |
| self-healing | repaired | repair was never re-run |
| run status | `succeeded` | 0 tests ran, 4 compile errors, report headlined BLOCKED |
| Slack notification | ✅ succeeded | same run, same lie, in the channel |
| authenticated crawl | 5 pages found | 5 copies of the login page |

The practical rules that fall out of it:

- **A verdict of "unverified" is a first-class outcome.** `ran=False` is not
  `passed`. A checker that could not start says so.
- **`RunStatus.BLOCKED` exists.** `succeeded` means verified automation.
  `failed` means the platform broke. `blocked` means it worked and produced
  nothing it can vouch for — a human must decide. CI exits non-zero on blocked.
- **Only claim what was observed.** A locator must come from the crawl. An
  assertion may reference a page, a title or an element that was seen — nothing
  else. A validation message seen on a page is evidence the page validates
  something, not evidence of what triggers it, so it never becomes an assertion.
- **Refuse rather than approximate.** An unbindable step is reported, not bound
  to the nearest thing that compiles.

---

## 3. Layout

```
agents/          the 12 pipeline stages, one package each
  orchestrator/  graph.py (built-in) and langgraph_graph.py — kept in lockstep
services/
  agent_engine/  run lifecycle, intents, deterministic chat answers
  gateway/       CommandEnvelope in, Reply out — the channel-independent brain
  discovery/     URL → features → scenarios → page objects (the autopilot path)
  knowledge_service/  ApplicationMap, repository index, standards
  api_gateway/   FastAPI: REST, WebSocket, SSE, channel webhooks
  model_router/  provider routing, budgets, capability tiers
  execution_service/  static validation, step coverage, gap resolution
packages/
  aiqa_types/    domain models and enums (RunStatus, RunMode, …)
  security/      workspace guard, command allowlist, secret redaction
tools/           filesystem, shell, git, Playwright, Jira, Slack/Teams
apps/
  vscode-extension/   sidebar chat + workbench panels
  control-plane/      web dashboard
scripts/demo_app.py   a real application to point it at
docs/PIPELINE.md      how a run actually flows — read this second
```

174 Python modules, 16 TypeScript modules, **739 tests passing, 3 skipped**,
`ruff` clean.

---

## 4. The pipeline

```
requirement → repository → exploration → test_design → code_generation
  → step_coverage → standards → execution → failure_analysis
  → self_healing → commit → reporting
```

Modes: `plan_only`, `generate`, `full`, `autonomous`, `execute_only`,
`heal_only`.

Two things are worth understanding before changing anything:

**`step_coverage` sits between generation and standards** because a step that
does nothing is not a style problem. It walks a four-rung ladder — existing
step, existing page method, observed locator, then *decline* — and there is no
rung that invents an interaction.

**The autopilot path bypasses the model entirely.** Given a URL,
`requirement` writes a requirement with *no acceptance criteria*, exploration
fills them in from the crawl, and `test_design` fails loudly if the crawl found
nothing. Scenarios and page objects are then rendered deterministically from the
discovered features. This is not an optimisation: a model asked to turn observed
features into Gherkin produced `Then the resident should exist in the database`
for an application with no database step.

---

## 5. What works, measured

Against `scripts/demo_app.py` — a five-route application that enforces a login.

| | result |
|---|---|
| Sign-in and authenticated crawl | 7 pages, 47 trusted locators, system Chrome |
| Features derived | 6 |
| Scenarios planned | 15, no model call |
| Page objects generated | 6, deterministic |
| Steps bound | 26, zero unbound |
| **Suite executed** | **13 of 17 passing** |

Chat: deterministic answers in 135–196 ms; streamed first token ~3.4 s.

The crawler signs in, verifies the sign-in actually took, captures the login
page before leaving it, and never follows a link that ends its session.
Credentials are stripped from the message before it becomes the run's
instruction, stored in the project's `.aiqa/` (git-ignored on creation), never
in the control-plane database and never in a prompt. The generated suite reads
`AIQA_APP_USERNAME` / `AIQA_APP_PASSWORD` at run time, so it can be committed.

---

## 6. What does not work yet

Be precise about this; it is the part that matters for planning.

**Four of seventeen demo scenarios fail.**

1. **Two are honest refusals.** `TC-AUTO-014/015` need `expectTitle()` on
   `DashboardPage` / `ReportsPage`, which are *hand-written* in the demo repo.
   The platform will not overwrite hand-written files, so it reports the step as
   pending and lists the methods the class does have. Working around this would
   mean overwriting somebody's code. If you want these green, the answer is
   probably to let autopilot generate its own class under a different name when
   the existing one cannot support the plan — not to relax the rule.

2. **One was a fixture problem, fixed but unverified end to end.** The happy
   path on a create form passed once and failed afterwards: it created a
   resident with a fixed email, and the application rejects duplicates. Sample
   emails now carry a `{unique}` token the step definition expands per
   execution. The unit tests pass; a full run has not yet confirmed it.

3. **One shifts around.** Between runs, a failure moved from one scenario to
   another as classification changed. Expect one or two genuine defects still in
   the generation path.

**Other known gaps:**

- **List content is invisible to the crawl.** `harvest()` collects interactive
  elements only, so table rows are never captured and `_renders_rows` is almost
  always false. Listing scenarios therefore rarely generate. Fixing this means
  capturing a few structural facts (row counts, headings) alongside elements.
- **Navigation scenarios are deliberately not generated.** The crawl records
  which routes it reached by following links but not the link *text*, so there
  is nothing to bind "I follow the X link" to.
- **The HTTP fallback cannot sign in.** When no browser starts, the crawler
  reads static HTML; that path has no session and says so.
- **Docker/Redis work is paused** at the user's request. The durable queue
  (`services/task_service/queue.py`) falls back to in-process with a warning.
- **Postgres is a code path, not a tested deployment.**
- **Mobile (Appium) and visual regression are scaffolded, not proven.**

---

## 7. Environment traps that cost real time

- **Playwright's bundled Chromium is missing on the dev machine** (wants
  chromium-1243, has 1223) and the CDN download times out. The crawler falls
  back to system Chrome, which works. The fallback now reports which browser did
  the work — if you see `simulated: true`, read the reason attached to it.
- **`npx` is not directly spawnable on Windows.** `tools/shell/shell_tools.py`
  resolves executables via `shutil.which` before `subprocess.run(shell=False)`.
  Removing that breaks every toolchain check silently.
- **Two control planes on one port.** Windows permits the double bind. The
  `serve` guard now probes the socket rather than an HTTP endpoint, because a
  busy server answered `/api/health` too slowly and a second instance started
  and marked the first one's live run as failed.
- **Restart the control plane after changing agent code.** Several measurements
  in development were taken against a stale build before this was noticed.
- **The demo app holds state in memory.** Restart it between comparable runs, or
  created records accumulate and skew results.

---

## 8. Model configuration

`configs/models.yaml`, version 4. Capability tiers: `reasoning`, `coding`,
`cheap`, `embedding`, `interactive_chat`.

**Only free OpenRouter models are configured, by the user's explicit
instruction, across two API keys that rotate on rate limit.** Do not introduce a
paid model without asking. `interactive_chat` has its own policy — 5 s ceiling,
300 output tokens, no retries — because chat has to feel instant while
automation stages are allowed minutes.

Free models are unreliable at long structured output. You will see
`could not obtain valid JSON after 2 attempts` in logs. That is a large part of
why the autopilot path was made deterministic.

---

## 9. What to build next

In the order I would do it:

1. **Finish the demo to green.** Verify the `{unique}` fix end to end, then
   decide the hand-written-page-object question. Getting a known-good baseline
   to 17/17 is worth more than any new feature, because every future change is
   measured against it.
2. **Capture list content during the crawl** — row counts and headings. This
   unlocks listing scenarios, which are a large share of any real application,
   and is the single biggest coverage gap.
3. **Link text during the crawl**, which unlocks navigation scenarios.
4. **Phase 6 — Postgres and an artifact store.** The code path exists; it needs
   a tested deployment. Paused with Docker.
5. **Phase 7 — execution isolation.** Runs currently share the working tree; two
   concurrent runs on one project would interleave writes.
6. **Outbound channel replies.** The gateway receives from Slack/Teams/WhatsApp
   but a finished run does not reply into the thread that started it. The run
   already records its originating `session_key`; what is missing is per-channel
   delivery, which needs credentials this environment does not have.
7. `docs/ROADMAP.md` has the longer list — API/DB test generation, real Appium,
   coverage-gap analysis, self-improving locators.

---

## 10. Working agreements to preserve

- **Push every time.** The user asked for this explicitly.
- **Free models only** until told otherwise.
- **Do not discard working code** when adding to it. A `docker-compose.yml` was
  overwritten once and had to be restored from git; the worker service was then
  added surgically instead.
- **Fix the bug, then pin it with a test that fails if it regresses.** Every
  entry in the table in §2 has one.
- **Run it, do not reason about it.** Nearly every defect in that table was
  found by pointing the platform at a real application and reading what came
  back. None were found by reading code.

---

## 11. Getting it running

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
python -m scripts.demo_app --port 8123          # the app under test
python -m services.api_gateway.cli serve        # the control plane
```

Dashboard at `http://127.0.0.1:8080/`, API key from `aiqa init` (development
default: `aiqa_dev_bootstrap_key_change_me`).

```bash
pytest tests -q          # 739 pass, 3 skip
ruff check .
```

VS Code extension: `cd apps/vscode-extension && npm install && npm run compile`,
then <kbd>F5</kbd>, then <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Q</kbd>.

Read next: `docs/PIPELINE.md` for how a run flows, `docs/ARCHITECTURE.md` for
the layers, `docs/ROADMAP.md` for the long list.
