# Manual testing guide

Everything below is already set up on this machine. This is the path to follow
to drive it yourself, and what to expect at each step.

The platform is configured for **free OpenRouter models only**. No paid model
can be selected — see [Cost safety](#cost-safety) for why that is enforced
rather than merely intended.

---

## 1. Start the two background processes

Two terminals, both from the repository root
(`C:\Users\Pavan.Kolla\Desktop\QA_AI_Agents`).

**Terminal 1 — the control plane.** The VS Code extension is a client of this;
without it the sidebar is a wall of errors.

```bash
.venv/Scripts/python.exe -m services.api_gateway.cli serve --port 8080
```

You should see a panel listing the API, docs and dashboard URLs, and
`providers=mock, hashing, ollama, openrouter`.

**Terminal 2 — a sample application to test against.** Any web app works; this
one is included so you have something to point at immediately.

```bash
.venv/Scripts/python.exe -m scripts.demo_app --port 8123
```

It serves `/login`, `/dashboard`, `/residents`, `/residents/new`, `/reports`.
It also has a deliberate defect, which is useful later — see step 6.

> The extension can start the control plane for you (`aiqa.autoStartServer` is
> on by default). Starting it by hand is only so you can watch the log while
> testing.

---

## 2. Open the demo project in VS Code

```bash
code C:\Users\Pavan.Kolla\Desktop\aiqa-demo
```

A small Playwright + Cucumber repository with an existing `LoginPage`,
`BasePage` and `login.feature`. It is there so you can see the platform *reuse*
existing conventions rather than inventing its own.

The extension (`ai-qa-engineer-0.1.0`) is already installed. You should see a
beaker icon in the Activity Bar.

---

## 3. Point the extension at the control plane

Open Settings (`Ctrl+,`), search `aiqa`, and set:

| Setting | Value |
|---|---|
| `aiqa.serverUrl` | `http://127.0.0.1:8080` |
| `aiqa.apiKey` | `aiqa_dev_bootstrap_key_change_me` |
| `aiqa.projectId` | `prj_d25f9cf368884ecb` |

The project is already registered against
`C:\Users\Pavan.Kolla\Desktop\aiqa-demo` with base URL `http://127.0.0.1:8123`.

To register a different repository instead, use **AI QA: Register Project**
from the Command Palette (`Ctrl+Shift+P`) and the extension fills the id in for
you.

Check it worked with **AI QA: Check Connection and Providers**. Every tier
should show an `openrouter/...:free` model.

---

## 4. Run something

Command Palette → **AI QA: Automate** → describe a feature, for example:

> Automate the Resident Registration functionality

What happens, in order: requirement analysis → repository indexing →
application exploration → test design → *approval gate* → code generation.

**Expect it to take 5–12 minutes.** Free models are slow — single calls run
60–170 seconds. This is the main trade for spending nothing; a paid tier does
the same work in about a minute.

The status bar names the agent as it goes — `requirement` (8%) → `repository` →
`exploration` (30%) → `test_design` (42%) → `code_generation` (60%). If it sits
on one agent for two minutes that is normal, not a hang; the **Activity** panel
and the control-plane terminal both show what it is waiting on.

At the approval gate the sidebar shows the proposed test plan. Review the
Gherkin, then approve or reject. Nothing is written to disk before you approve.

Afterwards you should have:

```
tests/features/resident-registration.feature
tests/pages/ResidentRegistrationPage.ts
tests/steps/resident-registration.steps.ts
tests/api/resident-registration.api.spec.ts
tests/api/resident-registration.db.spec.ts
```

---

## 5. What to look at first

The generated Page Object is the best single indicator of whether it worked:

- Locators should all be `resident-*` — **no `login-*` ones**. A Page Object
  models one route, and mixing routes was a real bug this catches.
- No `username2` / `password2` duplicates.
- `residentType` should use `.selectOption()`, not `.fill()` — it is a
  `<select>`.
- `readonly path` should be `/residents/new`.

In the steps file, every call should match its method's signature, and the
declared parameters should match the `{string}` placeholders in the pattern.
Any step the platform could not bind is a `TODO(aiqa)` comment naming exactly
what is missing — never a call to an identifier that does not exist. A step
that compiles and tests the wrong thing is worse than one that obviously needs
finishing.

If you do find a generated file that does not compile, that is a bug worth
reporting rather than something to work around — three such bugs were found and
fixed by reading the output of live runs, and there may be more.

`tests/api/*.api.spec.ts` calls only endpoints the crawler actually observed.
Write endpoints are `test.fixme` until you supply a request body, because
posting `"<fullName>"` would fail for a reason unrelated to the application.

---

## 6. Try the parts that need no requirement

```bash
# What the suite does NOT cover, worst gaps first
.venv/Scripts/python.exe -m services.api_gateway.cli knowledge coverage prj_d25f9cf368884ecb

# Probe the running app for self-evident defects, with no spec at all
.venv/Scripts/python.exe -m services.api_gateway.cli explore prj_d25f9cf368884ecb

# Per-test verdicts and quarantine advice (needs execution history first)
.venv/Scripts/python.exe -m services.api_gateway.cli suite-health prj_d25f9cf368884ecb
```

`explore` should report **one confirmed finding**: the demo app's navigation
links to `/logout`, which returns 404. That is a genuine defect in the app
under test, found with no requirement and no LLM call.

The same three are in the sidebar: coverage gaps appear under **Knowledge**
(click one to start the run that closes it), suite health under **Healing**,
and **AI QA: Run Exploratory Pass** in the Command Palette.

---

## 7. Actually run the generated tests

The platform generates tests but cannot execute them until the project has its
toolchain:

```bash
cd C:\Users\Pavan.Kolla\Desktop\aiqa-demo
npm install
npx playwright install
```

Then re-run with mode **Run Tests**, and failure analysis and self-healing have
something real to work on. Until then runs report *"execution blocked:
Playwright is not installed"*, which is accurate rather than a failure.

---

## Testing any page of your own

Nothing above is specific to the demo app. To point it at some page X:

**1. Make a repository for the tests.** It can be completely empty — this was
verified from scratch. If it already has Playwright tests, better: the platform
copies the conventions it finds instead of inventing its own.

```bash
mkdir C:\path\to\x-tests && cd C:\path\to\x-tests && git init
```

**2. Register it against the page.**

```bash
.venv/Scripts/python.exe -m services.api_gateway.cli project add "x-app" "C:\path\to\x-tests" --base-url "https://your-app.example.com/the/page"
```

A deep link is fine. The crawler starts there and follows the links it finds, so
pointing at `/residents/new` also discovered the login page that links back to
it. The command prints a project id.

**3. Point the extension at it.** Set `aiqa.projectId` to that id
(`Ctrl+,` → search `aiqa`), or run **AI QA: Register Project** from the Command
Palette, which does both steps and fills the id in for you.

**4. Run it.** Palette → **AI QA: Automate** → describe the feature in the terms
your team uses:

> Automate the resident registration form: valid submission, required-field
> validation, and duplicate email rejection

Naming the cases you care about matters more than the wording. "Test the page"
produces a vague plan; the sentence above produces the three scenarios asked
for.

**5. Review the plan, then approve.** Nothing reaches disk before you do.

### What decides whether this works well

| | |
|---|---|
| **`data-testid` attributes** | The single biggest factor. With them the locators are stable. Without, it falls back to text and role selectors, which break when copy changes. |
| **Reachable without login** | The crawler has no credentials. If the page sits behind auth it sees the login screen and nothing else: the catalogue comes back nearly empty and generation degrades to scaffolding. |
| **An existing test suite** | Optional but valuable — base classes, fixtures and naming are detected and reused rather than reinvented. |

The login limitation is the one most likely to bite you. There is no
authenticated-crawl support yet, so test a publicly reachable page first to see
the pipeline work end to end.

### A note on other people's sites

Point this at applications you own or are authorised to test. It sends real
requests, and the exploratory pass deliberately submits empty forms. Sites
published specifically for automation practice are fine; someone else's
production app is not.

---

## Cost safety

Your two keys are in `.env`, which is gitignored and on the tool layer's deny
list, so no agent can read it and nothing redacted reaches a model.

One of the two accounts (`...90b9`) is **not** free-tier — OpenRouter reports
`is_free_tier: false`, meaning it has credit on it. That account therefore gets
1000 free-model requests a day instead of 50, which is why it is the primary
key. It also means a paid model would spend real money.

So "free only" is enforced, not assumed. `configs/models.yaml` sets
`free_only: true`, and the router refuses to select any model with a non-zero
price whatever the tiers list. A cost ceiling could not express this: a ceiling
of zero *disables* the check rather than enforcing it.

Every run so far has reported `$0.000000`.

### When keys hit a limit

Both keys are configured and the platform rotates between them. It distinguishes:

- **burst limit** — cool down about 45 seconds, switch to the other key;
- **daily allowance spent** — park that key until midnight;
- **a busy model** — park that *model* for 30 seconds and fall through to the
  next one in the tier, because one model being rate limited says nothing about
  the next.

That last distinction matters: conflating it with a provider failure made an
entire run silently fall back to the offline stub while still reporting success.

---

## When free models misbehave

They are less reliable than paid ones, in specific and now-handled ways:

| What you may see | What the platform does |
|---|---|
| `does not support feature: structured-outputs` | Notes that model and retries immediately without the parameter |
| Reply cut off mid-JSON | Retries with a larger output budget and asks for a terser answer |
| `Provider returned error ... 429` | Parks that model briefly, uses the next in the tier |
| Every model unavailable | Falls back to the deterministic offline provider — the run still completes, but the output is scaffolded rather than designed |

That last row is worth knowing: a run can succeed having spoken to no model at
all. The trace in the sidebar names the provider and model for every call, so
`mock/mock-*` entries tell you it degraded.

### The model list goes stale

Free models on OpenRouter appear, become paid, and get retired constantly —
every model in the previous version of `models.yaml` had stopped being free.
When runs start failing for no obvious reason:

```bash
.venv/Scripts/python.exe -m scripts.qualify_models
```

It sends every free model the two requests this platform actually makes and
reports which return usable JSON. Update `configs/models.yaml` from its output.

---

## Known rough edges

- **Speed.** 5–12 minutes per run on free models, against roughly a minute on a
  paid tier. Almost all of it is waiting on model calls.
- **Retrieval quality.** There is no free embedding API, so repository search
  uses a local hashing embedder. It works; it is weaker than a real embedding
  model at finding semantically similar code.
- **Design output varies between runs.** Free models are less consistent than
  paid ones, so two runs of the same instruction can differ more than you would
  expect. The deterministic parts — rendering, validation, locator binding — do
  not vary.
