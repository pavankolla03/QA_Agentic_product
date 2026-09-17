# Getting started

A 10-minute path from clone to a reviewed, generated test suite.

## 1. Install

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # macOS / Linux
pip install -r requirements.txt
python -m services.api_gateway.cli init
```

`init` creates the schema and prints your API key. Keep it.

## 2. Check the environment

```bash
python -m services.api_gateway.cli doctor
```

This tells you which providers are reachable and how each capability is routed. If everything
resolves to `mock`/`hashing`, you are in offline mode — fine for evaluation, see step 6 to improve it.

## 3. Register your QA repository

```bash
python -m services.api_gateway.cli project add my-e2e C:/work/my-qa-repo --base-url http://localhost:3000
python -m services.api_gateway.cli project list
```

`--base-url` is the **application under test**, not the platform. Providing it is what enables live
locator discovery, and it makes a large difference to output quality.

Never pass a database connection string. If you want DB assertions, pass the *name* of an
environment variable: `--db-ref QA_DATABASE_URL`.

## 4. Teach it your conventions

```bash
python -m services.api_gateway.cli project index <project-id>
```

Prints what it learned: layout, base class, fixtures, naming, test-id scheme. If this looks wrong,
generated code will look wrong — fix the repository layout or add `.aiqa/standards.yaml` before
going further.

Optionally audit what you already have:

```bash
python -m services.api_gateway.cli project lint <project-id>
```

## 5. Automate a feature

```bash
python -m services.api_gateway.cli run start <project-id> "Automate the Resident Registration functionality"
```

The run stops at the test-plan gate and prints the approval command. Review the Gherkin, approve,
review the code diff, approve. Nothing touches your workspace until you say so.

Start conservatively:

```bash
--mode plan_only     # design only, writes nothing
--mode generate      # writes code, does not execute
--mode full          # generate + execute + analyse   (default)
--mode autonomous    # also self-heals and re-runs
```

## 6. Improve quality with a real model

**Free and private (recommended first step):**

```bash
ollama serve
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
```

**Free and hosted:** get an OpenRouter key and set `OPENROUTER_API_KEY` in `.env`.

**Best quality:** set `ANTHROPIC_API_KEY` — `configs/models.yaml` already routes reasoning and
coding to Claude first, with local models as the fallback.

Restart the server; `doctor` will show the new routing.

## 7. Use it from VS Code

```bash
cd apps/vscode-extension
npm install && npm run compile
```

Press F5, then Ctrl+Alt+Q in the Extension Development Host. Run
**QAgentic: Register This Workspace as a Project**, and paste the API key when prompted (it goes into
the OS keychain, not settings.json).

## 8. Dashboard

<http://127.0.0.1:8080> — approvals, runs, per-agent traces, spend, flaky tests and the audit log.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Cannot reach the QAgentic control plane` | The server is not running. `python -m services.api_gateway.cli serve` |
| Everything routes to `mock` | No provider reachable. Start Ollama or set a provider key, then restart |
| `Playwright is not installed in this project` | In your QA repo: `npm install && npx playwright install` |
| Generated locators are all `TODO(aiqa)` | The app was unreachable. Set the project's `base_url` and make sure it is running |
| `Writing '...' is outside the allowed test directories` | Working as intended — agents may only write test assets. Adjust `write_allow_globs` in `configs/security.yaml` if your layout differs |
| Standards errors block a run | Review them; a human can still approve with acknowledgement, or relax the rule in `.aiqa/standards.yaml` |
| Run stops with `budget_exceeded` | Raise `AIQA_PER_RUN_COST_LIMIT_USD`, or the daily/monthly ceiling |
