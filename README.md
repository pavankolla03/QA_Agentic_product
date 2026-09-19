# QAgentic

**A cost-optimized autonomous QA engineering platform.** You describe a feature —
or you just paste a URL. The platform reads your repository, explores the running
application, designs a risk-based test plan, writes Playwright + BDD + Page Object
automation that matches your team's conventions, runs it, diagnoses failures,
repairs the ones that are test problems, and reports what it did and what it cost.

The QA engineer is the **reviewer and approver**, not the typist.

Give it nothing but a URL and the application becomes the specification: it
crawls what is there, names the features it can actually see — a form with a
password field is a sign-in, a required field is a rejection path — and automates
those. What it cannot see, it does not write a test for, and it says so. Reach it
from VS Code, Slack, Teams, WhatsApp, a voice call, CI or the API; all of them go
through one gateway, so a message means the same thing wherever you send it.

The central design constraint is cost: **do not ask an LLM to rediscover what the
platform already knows.**

```
                          QA ENGINEER
                               │
       ┌───────────┬───────────┼───────────┬───────────┐
       ▼           ▼           ▼           ▼           ▼
    VS Code   Slack/Teams   WhatsApp     voice      CI/API
       └───────────┴───────────┼───────────┴───────────┘
                               ▼
              ┌────────────────────────────────┐
              │      INTERACTION GATEWAY       │
              │ one envelope in, one reply out │
              └────────────────┬───────────────┘
                               │ REST + WebSocket
                               ▼
              ┌────────────────────────────────┐
              │     QAgentic CONTROL PLANE     │
              │ auth · RBAC · runs · traces    │
              │ tokens · cost · approvals      │
              │ audit · metrics                │
              └────────────────┬───────────────┘
                               ▼
              ┌────────────────────────────────┐
              │   ORCHESTRATOR  (LangGraph)    │
              └────────────────┬───────────────┘
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
         KNOWLEDGE LAYER  MODEL ROUTER    TOOL LAYER
                │              │              │
         Repository Map   reasoning       Playwright
         Application Map  coding          Git · Shell
         Standards        cheap           API · Database
         Test Knowledge   embedding       Mobile · Jira
         QA Graph         (+ no-LLM)      Slack · Teams
                               │
                               ▼
                       EXECUTION RESULTS
                    ┌──────────┴──────────┐
                  PASS                  FAIL
                    │                     │
                    │             FAILURE ANALYSIS
                    │                     │
                    │              SELF-HEALING
                    │                     │
                    └──────────┬──────────┘
                               ▼
                           REPORTING
                    Slack · Teams · Dashboard
```

---

## Quick start

```bash
git clone <this-repo> && cd QA_AI_Agents

python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt

python -m services.api_gateway.cli init
python -m services.api_gateway.cli serve
```

Dashboard at <http://127.0.0.1:8080>, API docs at `/docs`.

**No API key and no Ollama?** It still runs. A deterministic offline engine and a
local hashing embedder produce a complete run — plan, Gherkin, page objects,
report — so you can evaluate the platform before configuring anything.

### Prove it works

```bash
python -m scripts.selfcheck        # end-to-end, zero credentials
python -m scripts.benchmark        # measure the cost of repeat runs
pytest tests -q                    # the whole suite
```

### Automate something

```bash
aiqa project add my-e2e ./path/to/qa-repo --base-url http://localhost:3000
aiqa project index <project-id>          # learn the repository's conventions
aiqa standards init ./path/to/qa-repo    # scaffold .aiqa/ (optional, recommended)
aiqa run start <project-id> "Automate the Resident Registration functionality"
```

The run stops at the first approval gate and prints the command to approve it.

Naming a feature gives a sharper plan, but it is not required. A URL on its own
is a complete instruction:

```bash
aiqa run start <project-id> "http://localhost:3000"
```

Almost everything worth testing sits behind a sign-in, so give it an account
too. The credentials are stripped from the text before it becomes the run's
instruction, kept in the project's own `.aiqa/` directory (git-ignored on
creation) rather than the control-plane database, and never enter a prompt:

```bash
aiqa run start <project-id> "http://localhost:3000 user: qa.bot pass: <password>"
```

The generated suite reads `AIQA_APP_USERNAME` and `AIQA_APP_PASSWORD` at run
time, so it can be committed and run in CI without a password in the repository.
If the sign-in is refused the run stops and says so, rather than crawling the
login page repeatedly and presenting it as the application.

The application is then the specification. Exploration crawls it, names the
features it can see, and those become the acceptance criteria — so the plan
covers what is actually there rather than what a model expects a site like that
to have. If the crawl reaches nothing, the run fails naming the URL instead of
inventing a suite for a page it never loaded.

### From Slack, Teams, WhatsApp, voice or CI

Every channel posts to the same gateway and gets the same understanding of what
was said. `GET /api/channels` lists the endpoints.

```bash
curl -X POST http://localhost:8080/api/channels/slack   -H "X-API-Key: $AIQA_API_KEY" -H 'Content-Type: application/json'   -d '{"event": {"text": "http://localhost:3000", "user": "U1", "channel": "C1"}}'
```

Webhooks authenticate with the platform's own API key, not with the provider's
payload: a Slack body says which Slack user is speaking, never what they are
allowed to start.

A conversation remembers its project after the first message. In an installation
with several projects, one that has never named a project is asked rather than
pointed at whichever was registered most recently.

### VS Code

```bash
cd apps/vscode-extension && npm install && npm run compile
```

<kbd>F5</kbd> to launch an Extension Development Host, then <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Q</kbd>.

---

## How cost is controlled

Full detail in **[docs/cost-optimization.md](docs/cost-optimization.md)**. The short version:

| Mechanism | Effect |
|---|---|
| **Deterministic tools** | File IO, git, npm, `tsc`, ESLint, parsing, cost math — never an LLM |
| **Four model tiers** | Paid reasoning only where judgement changes the outcome |
| **Complexity scoring** | Free, deterministic; downgrades trivial work, escalates hard work (capped) |
| **Repository Map** | Content-hashed; an unchanged repo costs **zero** reads and embeddings |
| **Application Map** | Explore once, not per scenario; confidence decay drives re-exploration |
| **Test Knowledge** | Reuse prior implementations; drop duplicate scenarios before generating |
| **Static-first validation** | `tsc`/ESLint/Gherkin before any semantic review |
| **Batch design** | One planning call for N scenarios, not N calls |
| **Request accounting** | Free tiers are request-limited; reroute before hitting the wall |
| **Three hard ceilings** | Requests, tokens and dollars — checked before every call |

```bash
aiqa cost report        # measured spend, unit economics, what caching avoided
aiqa knowledge show <project-id>
```

---

## What happens in a run

| # | Agent | Tier | Job |
|---|---|---|---|
| 1 | **Requirement** | reasoning | Free text or Jira → testable acceptance criteria, with an ambiguity score |
| 2 | **Repository** | cheap | Incremental index; layout, base classes, fixtures, naming; resolves layered standards |
| 3 | **Exploration** | cheap | Crawls only routes the Application Map cannot vouch for |
| 4 | **Test Design** | reasoning | Reuse-first, batched, risk-based Gherkin — **approval gate** |
| 5 | **Code Generation** | coding | Feature files rendered from the approved plan; POM + steps generated |
| 6 | **Standards** | cheap | Static checks first; LLM only for what tools cannot decide |
| 7 | **Execution** | cheap | Applies the approved diff — **approval gate** — then runs the suite |
| 8 | **Failure Analysis** | reasoning | Broken test, or broken product? |
| 9 | **Self-Healing** | reasoning | Minimal repairs — **approval gate** — verified by re-running |
| 10 | **Reporting** | cheap | Report + Slack/Teams; writes what it learned back into knowledge |

Modes: `plan_only`, `generate`, `full`, `autonomous`, `execute_only`, `heal_only`.

---

## What gets generated

| File | When | Notes |
|---|---|---|
| `tests/features/*.feature` | always | byte-for-byte the Gherkin that was approved; never written empty |
| `tests/pages/*Page.ts` | always | one route per Page Object; locators only from the verified catalogue |
| `tests/steps/*.steps.ts` | always | steps call Page Object methods; a step that cannot bind is a `TODO`, never broken code |
| `tests/data/*.json` | data-driven scenarios | Examples tables, extracted |
| `tests/api/*.api.spec.ts` | an observed endpoint is planned | Playwright `request` fixture; unobserved paths are dropped |
| `tests/api/*.db.spec.ts` | a database check is planned | against your own `queryRows` helper |
| `tests/visual/*.visual.spec.ts` | opt-in, and the route is settled | `toHaveScreenshot()`, dynamic regions masked |

---

## Beyond one feature at a time

| Command | What it does |
|---|---|
| `aiqa batch <project> --epic QA-100` | every issue under a Jira epic, priority-ordered, with a batch-wide cost ceiling and a preview before it spends anything |
| `aiqa knowledge coverage <project>` | what the suite does *not* cover — routes, endpoints, shared components, requirements — each with the run that would close it |
| `aiqa suite-health <project>` | per-test verdicts and quarantine advice; a test that never passes is `broken`, never quarantined |
| `aiqa explore <project>` | probes the running application with no requirement, looking for crashes, error pages, dead links and absent validation |

Each has a `--fail-on-*` flag so it can gate CI.

---

## Safety model

Enforced in code, not in prompts.

| Guarantee | Enforcement |
|---|---|
| Secrets never reach a model | Redaction at every prompt, log and trace boundary |
| Agents cannot read `.env`, keys, certs | `WorkspaceGuard` deny-list |
| Agents cannot escape the repository | Path resolution + root confinement; symlinks refused |
| Agents can only write test assets | Write allow-list — `src/` is unreachable |
| **Least privilege per agent** | `code_generation` cannot write; `self_healing` cannot commit; **nobody can push** |
| Only allow-listed executables run | `CommandGuard` + forbidden-argument patterns |
| Protected branches are safe | `GitGuard`; push disabled unless explicitly enabled *and* approved |
| The database is read-only | Single `SELECT` only; DSN comes from an env-var *name* |
| Real defects are never "healed" | A value-mismatch override outranks the model's classification |
| Repairs cannot weaken tests | Removing an assertion, adding a hard wait or inventing a locator is rejected |
| Spend is bounded | Per-run, daily and monthly ceilings, restored across resume |
| Everything is auditable | Append-only log of every write, command, commit, approval and denial |

```bash
aiqa permissions        # the least-privilege matrix
aiqa pipeline           # the agent graph, rendered from the compiled graph
```

---

## Standards as data

`.aiqa/` in your repository, merged **module > project > company**:

```
.aiqa/
├── config.yaml              machine-readable project standards
├── standards/*.md           your conventions in prose → parsed into rules
├── examples/                real files of yours → house style is learned from them
├── modules/<name>.yaml      per-area overrides
├── repository_map.json      generated; commit it so CI starts warm
└── application_map.json     generated; cached pages and locators
```

Write standards the way you'd say them:

```
All locators should use getByRole.
Steps cannot contain locators.
Assertions must remain outside Page Objects.
```

```bash
aiqa standards parse "Every scenario must be tagged."   # check before committing
aiqa standards show                                      # what is actually in force
```

Anything not recognised is **reported, not silently dropped**.

---

## Repository layout

```
apps/
  vscode-extension/      12 panels, 34 commands, chat, diff review
  control-plane/         web dashboard (vanilla JS, no build step)
services/
  api_gateway/           FastAPI + the `aiqa` CLI
  agent_engine/          run lifecycle, durable suspend/resume
  model_router/          tiers, budgets, quotas, fallback
  knowledge_service/     repository map · application map · test knowledge · standards
  execution_service/     static validation pipeline
  observability/         ORM, tracing, cost, metrics dashboards
agents/                  orchestrator (LangGraph) + 10 specialized agents
tools/                   31 policy-guarded tools
packages/
  aiqa_types/            domain models + budgets
  security/              redaction, guards, RBAC
  agent_protocol/        control-flow signals + agent permissions
  llm_provider/          provider abstraction + offline engine
configs/                 models.yaml · standards.yaml · security.yaml
scripts/                 selfcheck · benchmark · demo_app
tests/                   unit, integration, end-to-end
```

---

## Deployment

```bash
cd infrastructure/docker
cp ../../.env.example .env       # set AIQA_SECRET_KEY and AIQA_BOOTSTRAP_API_KEY
docker compose up -d                      # control plane + Postgres + Redis
docker compose --profile local-llm up -d  # …plus Ollama, fully private
```

SQLite by default; PostgreSQL is a connection-string change.

---

## Documentation

- **[Handover](docs/HANDOVER.md)** — what works, what does not, and what to build
  next. Start here if you are new to the project.
- **[Pipeline](docs/PIPELINE.md)** — how a run actually flows, stage by stage
- **[Manual testing guide](docs/MANUAL_TESTING.md)** — the configured setup on this machine, start to finish
- **[Getting started](docs/GETTING_STARTED.md)** — clone to first suite in ~10 minutes
- **[Cost optimization](docs/cost-optimization.md)** — how the spend is controlled, and how to tune it
- **[Architecture](docs/ARCHITECTURE.md)** — why it is built this way, and how to extend it
- **[Roadmap](docs/ROADMAP.md)** — what is next, and what will not be automated

---

## Current limitations

Stated plainly, because a QA tool that overstates itself is worse than useless.

- **Mobile is architecture, not execution.** The capability contract and scaffolding
  exist; running against a real Appium grid does not.
- **API tests only reach endpoints the application revealed.** Endpoints are taken
  from form actions observed during exploration. One reached only by client-side
  `fetch` is absent rather than guessed at, and a write endpoint is generated as
  `test.fixme` until someone supplies a valid request body.
- **Database checks are parameterised, not connected.** The platform has no schema
  and no credentials, so it emits the table, predicate and expected row count
  against a `queryRows` helper the project supplies. Until that exists the file
  does not compile — deliberately, because a database check that silently does
  nothing is worse than none.
- **Visual regression is off by default.** A screenshot baseline is a commitment
  to review it on every intentional design change. Enable it in `standards.yaml`,
  and only routes whose markup repeated across explorations are baselined.
- **Exploration needs a reachable application.** Without one, locators are marked
  `TODO(aiqa)` rather than invented.
- **Offline mode is a floor, not a substitute.** It guarantees a working pipeline
  with no credentials; design quality is materially better with a real model.
- **Self-healing is deliberately conservative.** It repairs locators, timing, data
  and step logic — and refuses anything resembling a product defect, even when
  that means leaving a test red.
- **Cost savings are measured, not projected.** `scripts/benchmark.py` reports real
  request and token counts; dollar figures require `--live` with your own providers.
- **Exploratory testing finds only self-evident failures.** With no requirement to
  check against, it reports crashes, server error pages, dead links and absent
  validation on required fields. A clean pass is not a claim that the application
  is correct.
- **Coverage means "a test touches this", not "this is well tested".** The report
  says so on every screen it appears on.
