# AI QA Engineer

**An autonomous QA engineering platform.** You describe a feature; the platform reads your
repository, explores the running application, designs a risk-based test plan, writes Playwright +
BDD + Page Object automation that matches your team's conventions, runs it, diagnoses failures,
repairs the ones that are test problems, and reports what it did and what it cost.

The human QA engineer is the **reviewer and approver**, not the typist.

```
┌──────────────────────────────────────────────────────────────────┐
│  QA ENGINEER — reviews diffs, approves gates, owns the decisions │
└────────────────────────────────┬─────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  1. VS CODE EXTENSION                                            │
│     chat · commands · diff review · approvals · live cost        │
└────────────────────────────────┬─────────────────────────────────┘
                                 ▼  HTTPS + WebSocket
┌──────────────────────────────────────────────────────────────────┐
│  2. CONTROL PLANE / OBSERVABILITY                                │
│     auth · RBAC · projects · runs · traces · tokens · cost       │
│     audit · metrics · model routing · notifications              │
└────────────────────────────────┬─────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  3. AGENT ENGINE — resumable state graph                         │
│     Orchestrator                                                 │
│     ├── Requirement          ├── Standards / Governance          │
│     ├── Repository           ├── Execution                       │
│     ├── Exploration          ├── Failure Analysis                │
│     ├── Test Design          ├── Self-Healing                    │
│     └── Code Generation      └── Reporting                       │
└────────────────────────────────┬─────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  4. TOOL LAYER — 31 deterministic, policy-guarded tools          │
│     Playwright · API · Database · Mobile · Git · Shell · Jira    │
│     Slack · Teams · Filesystem (workspace-confined)              │
└──────────────────────────────────────────────────────────────────┘
```

**The LLM reasons. Deterministic tools act.** Every side effect passes a policy guard, is traced,
and is auditable.

---

## Quick start

```bash
git clone <this-repo> && cd QA_AI_Agents

python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

cp .env.example .env              # optional — it runs with no configuration at all
python -m services.api_gateway.cli init
python -m services.api_gateway.cli serve
```

Open <http://127.0.0.1:8080> for the dashboard, or <http://127.0.0.1:8080/docs> for the API.

**No API key and no Ollama?** It still works. The platform falls back to a deterministic offline
engine and produces a complete run — plan, Gherkin, page objects, report — so you can evaluate it
before configuring anything. Quality improves substantially once a real model is routed.

### Prove it works

```bash
python -m scripts.selfcheck
```

Runs the whole platform against a disposable copy of the bundled sample repository and asserts the
approval gates, workspace confinement, secret redaction, tracing and reporting all behave.

### Automate something

```bash
python -m services.api_gateway.cli project add my-e2e ./path/to/qa-repo --base-url http://localhost:3000
python -m services.api_gateway.cli project list                 # copy the project id
python -m services.api_gateway.cli project index <project-id>   # learn its conventions
python -m services.api_gateway.cli run start <project-id> "Automate the Resident Registration functionality"
```

The run stops at the first approval gate and prints the command to approve it.

### VS Code

```bash
cd apps/vscode-extension && npm install && npm run compile
```

Press <kbd>F5</kbd> to launch an Extension Development Host, then <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Q</kbd>.

---

## What actually happens in a run

| # | Agent | What it does | Why it is separate |
|---|-------|--------------|--------------------|
| 1 | **Requirement** | Turns free text or a Jira issue into testable acceptance criteria, and scores its own ambiguity | Vague requirements produce vague tests — better to surface the gap than to guess |
| 2 | **Repository** | Static analysis + embedding index: layout, base classes, fixtures, naming, test-id scheme | Generated code that ignores your conventions gets rewritten by hand |
| 3 | **Exploration** | Drives a real browser over the app and harvests **verified** locators | A locator observed in the live DOM passes; an imagined one does not |
| 4 | **Test Design** | Risk-based Gherkin traced to acceptance criteria — **first approval gate** | The plan is cheap to change; the code is not |
| 5 | **Code Generation** | Feature files, Page Objects, step definitions, test data | Feature files are rendered deterministically from the approved plan |
| 6 | **Standards** | Deterministic rule engine over every generated file, with safe autofixes | Standards enforced by regex, not by asking a model to be careful |
| 7 | **Execution** | Applies the approved diff — **second gate** — then runs the suite | The diff a human approves is the diff that lands |
| 8 | **Failure Analysis** | Classifies each failure: broken test, or broken product? | The single most valuable judgement in autonomous QA |
| 9 | **Self-Healing** | Minimal, evidence-based repairs — **third gate** — verified by re-running | Worst case is "no change", never "a suite that looks fixed" |
| 10 | **Reporting** | Report + Slack/Teams delivery. Numbers computed, prose written | A hallucination must not be able to misstate a result |

### Run modes

| Mode | Pipeline |
|------|----------|
| `plan_only` | requirement → repository → exploration → test design → report |
| `generate` | …→ code generation → standards → report |
| `full` | …→ execution → failure analysis → self-healing → commit → report |
| `autonomous` | as `full`, approvals batched |
| `execute_only` | run the existing suite and analyse failures |
| `heal_only` | diagnose and repair known failures |

---

## Safety model

These are enforced in code, not in prompts.

| Guarantee | Enforcement |
|-----------|-------------|
| Secrets never reach a model | `packages/security/redaction.py` — every prompt, log and trace is redacted at the boundary |
| Agents cannot read `.env`, keys or certs | `WorkspaceGuard` deny-list, checked before any read |
| Agents cannot escape the repository | Path resolution + root confinement; symlinks refused |
| Agents can only write test assets | Write allow-list (`tests/**`, `e2e/**`, …) — `src/` is unreachable |
| Only allow-listed executables run | `CommandGuard`, plus forbidden-argument patterns |
| Protected branches are safe | `GitGuard`; pushing is disabled unless explicitly enabled *and* approved |
| The database is read-only | Single-statement `SELECT` only; DSN comes from an env-var *name*, never a literal |
| Real defects are never "healed" | A value-mismatch safety override outranks the model's classification |
| Repairs cannot weaken tests | Proposals removing an assertion, adding a hard wait, or inventing a locator are rejected |
| Spend is bounded | Per-run, daily and monthly ceilings, checked before every call |
| Everything is auditable | Append-only audit log of every write, command, commit, approval and denial |

Try to break it:

```python
from packages.security import WorkspaceGuard, PolicyViolation
guard = WorkspaceGuard("./my-repo")
guard.resolve_read(".env")            # PolicyViolation: workspace.deny_path
guard.resolve_write("src/app.ts")     # PolicyViolation: workspace.write_allowlist
guard.resolve_read("../../secrets")   # PolicyViolation: workspace.root_confinement
```

---

## Model routing

Agents ask for a **capability**; the router resolves it to the first healthy provider and falls
back automatically. Swapping models is a config change, never a code change.

```yaml
# configs/models.yaml
routes:
  coding:
    - { provider: anthropic,  model: "claude-sonnet-5",     in: 3.00, out: 15.00 }
    - { provider: ollama,     model: "qwen2.5-coder:7b",    in: 0,    out: 0 }
    - { provider: openrouter, model: "qwen/...:free",       in: 0,    out: 0 }
```

Supported: **Ollama** (local, free), **OpenRouter** (many free models), **OpenAI**, **Anthropic**,
**Gemini**, plus a deterministic offline engine and a local hashing embedder that need nothing at all.

Cost is tracked per call, agent, model, provider, run, project, user, day and month.

---

## Organization standards

`configs/standards.yaml` defines the house rules; a project overrides them with
`.aiqa/standards.yaml` in its own repository. Rules are data — a QA lead can add one without
touching Python.

```yaml
rules:
  - id: ACME-001
    severity: error
    title: "Page Objects must extend BasePage"
    applies_to: [pages]
    kind: semantic
    check: extends_base_page
```

Regex rules (`detect:`) cover textual patterns; semantic checks handle what regex cannot — is every
scenario tagged, does this page object extend the house base class, is this helper already defined
elsewhere. Rules may declare a safe `autofix`.

Audit an existing suite without generating anything:

```bash
python -m services.api_gateway.cli project lint <project-id>
```

---

## Repository layout

```
apps/
  vscode-extension/       TypeScript extension — chat, diffs, approvals, cost
  control-plane/          Web dashboard (vanilla JS, no build step)
services/
  api_gateway/            FastAPI: auth, RBAC, runs, approvals, WS, metrics, CLI
  agent_engine/           Run lifecycle, durable suspend/resume
  model_router/           Capability routing, fallback, cost accounting
  knowledge_service/      Repository parsing, indexing, hybrid retrieval
  observability/          ORM, tracing, cost roll-ups, governance
agents/                   Orchestrator + 10 specialized agents
tools/                    31 policy-guarded tools
packages/
  aiqa_types/             Domain models shared across every boundary
  security/               Redaction, workspace/command/git guards, RBAC
  llm_provider/           Provider abstraction + offline engine
  agent_protocol/         Control-flow signals (approval vs failure)
configs/                  models.yaml · standards.yaml · security.yaml
infrastructure/           Dockerfile, compose, CI workflows
tests/                    159 tests: unit, integration, end-to-end
```

---

## Deployment

```bash
cd infrastructure/docker
cp ../../.env.example .env        # set AIQA_SECRET_KEY and AIQA_BOOTSTRAP_API_KEY
docker compose up -d                      # control plane + Postgres + Redis
docker compose --profile local-llm up -d  # …plus Ollama, fully private
```

SQLite is the zero-setup default; PostgreSQL is a connection-string change.

CI templates are in `infrastructure/ci/`:
`github-actions.yml` builds and tests the platform; `qa-pipeline.yml` shows how to use it in *your*
pipeline (nightly self-healing that opens a PR, and a QA standards gate on pull requests).

---

## Development

```bash
pytest tests -q                 # 159 tests
python -m scripts.selfcheck     # end-to-end
ruff check .
cd apps/vscode-extension && npm run compile
```

The suite runs fully offline and deterministically: no API keys, no network, no flakes.

`python -m services.api_gateway.cli doctor` reports which providers are reachable and how each
capability is currently routed.

---

## Current limitations

Stated plainly, because a QA tool that overstates itself is worse than useless:

- **Mobile is architecture, not execution.** The capability/locator contract and scaffolding are in
  place; running against a real Appium grid is not part of this scope.
- **Exploration needs a reachable application.** Without one, locators are marked `TODO(aiqa)`
  rather than invented — generated page objects will need a human pass.
- **Offline mode is a floor, not a substitute.** It guarantees a working pipeline with no
  credentials; test-design quality is materially better with a real model.
- **Self-healing is deliberately conservative.** It repairs locators, timing, data and step logic —
  and refuses anything that looks like a product defect, even when that means leaving a test red.
