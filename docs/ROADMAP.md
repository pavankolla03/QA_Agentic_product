# Roadmap — toward higher automation coverage

Where the platform is, and what it would take to move further. Ordered by
value-per-effort, with the honest constraint on each.

The goal is not "100% automation" as a slogan. Some QA work genuinely requires a
human: deciding whether behaviour is *correct*, judging whether a defect matters,
signing off a release. The goal is to automate everything that is mechanical and
to make the remaining human decisions cheap, well-evidenced and fast.

---

## Where it is now

| Capability | State |
|---|---|
| Requirement → Gherkin → POM → steps | Working, reuse-aware, standards-enforced |
| Live locator discovery | Working, cached, confidence-tracked |
| Execution + failure triage | Working, deterministic-first |
| Self-healing | Working, verify-or-revert, defect-safe |
| Cost control | 4 tiers, 3 ceilings, measured caching |
| Human-in-the-loop | Durable gates with real diffs |
| API testing | Tools present; generation is UI-first |
| Database validation | Read-only query + assertion tools |
| Mobile | Architecture and scaffolding only |
| CI/CD | Templates provided; no first-class run-from-CI UX |

**Honest coverage estimate:** a UI regression suite for a server-rendered or
conventionally-built SPA is largely automatable today, with a human reviewing
two gates per feature. Coverage falls where the application is unconventional
(canvas, heavy shadow DOM, no accessible names), where the requirement is
ambiguous, or where correctness depends on domain knowledge the platform has no
access to.

---

## Near term — finish what is architected

### 1. API and database test generation as first-class outputs
**Effort: medium. Value: high.**

The tools exist and the Test Design Agent already emits `api_checks` and
`db_checks`, but the Code Generation Agent only renders UI artifacts. Generating
API specs from OpenAPI and DB assertions from the schema would cover a large
slice of testing far more cheaply and reliably than driving a browser.

*Constraint:* needs the project to expose a spec or a read-only DSN. Both are
already modelled (`api_base_url`, `database_dsn_ref`).

### 2. Real Appium execution
**Effort: medium. Value: high for mobile teams, zero otherwise.**

`tools/mobile/` defines capabilities, locator priority and a scaffold. What is
missing is a driver, a device/emulator lifecycle, and mobile-specific locator
discovery. The agents need no changes — that was the point of the interface.

*Constraint:* requires an Appium grid in CI, which is an infrastructure decision
more than a code one.

### 3. Visual regression
**Effort: medium. Value: high.**

Screenshots are already captured. Adding baseline storage, perceptual diffing and
a "was this change intended?" approval gate would catch a class of defect that
DOM assertions structurally cannot.

*Constraint:* baseline management is the hard part, not the diffing. Needs a
per-branch baseline strategy or it becomes noise.

### 4. Run-from-CI as a first-class flow
**Effort: small. Value: high.**

`infrastructure/ci/qa-pipeline.yml` shows the shape, but it is curl-and-poll. A
small `aiqa ci` command that streams progress, returns a proper exit code and
emits a JUnit summary would make the platform a CI citizen rather than a
service CI talks to.

---

## Medium term — raise the ceiling on what can be automated

### 5. Requirement ingestion at scale
**Effort: medium. Value: high for teams with a backlog.**

Today a run automates one instruction. Pointing the platform at a Jira epic,
a Confluence spec or a requirements document and having it produce a coverage
*plan* across many features — with gaps and duplicates identified before any
generation — is the difference between "helps with a feature" and "clears a
backlog".

*Constraint:* needs the deduplication and knowledge-graph work that already
exists, plus a plan-level approval UI. The graph's `coverage_pct` is the
foundation.

### 6. Coverage-gap analysis
**Effort: small. Value: high.**

The knowledge graph already links requirements to tests and can compute
requirement coverage. Turning that into "these six acceptance criteria have no
test, and here is the plan to cover them" is mostly reporting work on data
already collected.

### 7. Self-improving locator strategy
**Effort: medium. Value: medium-high.**

`locator_health` records which strategies survive. That data can drive a
per-project ranking — a team whose app has excellent `data-testid` coverage
should get different generation from one that relies on roles. The table exists
and is populated; nothing consumes it yet.

### 8. Cross-browser and parallel execution strategy
**Effort: small–medium. Value: medium.**

Playwright projects make this mostly configuration, but deciding *which* tests
warrant a cross-browser matrix is a judgement the platform is well placed to make
from failure history.

### 9. Test suite health management
**Effort: medium. Value: high at scale.**

The `flaky_tests` ledger exists and recommends quarantine. The next steps are
acting on it: auto-quarantine with an approval gate, detecting redundant tests
that never fail independently, and flagging slow tests whose runtime is not
justified by their coverage.

---

## Longer term — genuinely harder problems

### 10. Autonomous exploratory testing
**Effort: large. Value: high, and genuinely novel.**

Rather than automating a stated requirement, drive the application looking for
problems: state-machine exploration, invariant checking, fuzzing form inputs
against validation rules the app itself declares. This is where an agent can do
something a human cannot do at scale.

*Constraint:* the oracle problem. Without a specification, "is this behaviour
wrong?" is undecidable in general. Practical scope: crashes, 5xx responses,
accessibility violations, broken invariants, and states the app claims are
impossible.

### 11. Production-signal-driven test generation
**Effort: large. Value: very high.**

Feed real user journeys (RUM, session replay, server logs) into the Test Design
Agent so coverage follows actual usage rather than a QA engineer's model of it.
The application map already stores routes and navigation; production data would
weight them by real traffic.

*Constraint:* PII. Journeys must be anonymised before they come anywhere near
the platform, and the redaction layer is the right place to enforce that.

### 12. Defect prediction from change impact
**Effort: large. Value: high.**

Given a code diff in the *application* repository, select the tests most likely
to be affected. The knowledge graph already links pages and components to tests;
the missing half is mapping application source files to those components.

### 13. Multi-repository and multi-team scale
**Effort: large. Value: high for enterprises.**

Shared component knowledge across repositories, org-wide standards inheritance,
cross-team reuse of page objects, and a portfolio view of coverage. The data
model is already multi-tenant; the retrieval and permission layers would need to
become cross-project.

---

## What will not be automated, and why

Worth stating plainly, because a roadmap that implies otherwise is dishonest.

**Deciding whether behaviour is correct.** The platform detects that expected and
actual differ. Whether the expectation or the application is wrong is a product
decision. This is why the failure analyser refuses to "heal" an assertion
mismatch — that refusal is a feature, not a gap.

**Judging defect severity.** Whether a bug blocks a release depends on business
context the platform does not have.

**Sign-off.** Someone accountable has to accept the risk. The platform's job is
to make that decision well-evidenced and quick, not to remove it.

**Novel test *ideas* in a domain the platform cannot observe.** It can generate
excellent coverage of what the application does and what the requirement says.
A tester's intuition that "finance will try this in a way nobody documented"
comes from somewhere the platform cannot see.

---

## Suggested sequence

If the aim is maximum coverage gain per unit of effort:

1. **Run-from-CI** (small, unlocks continuous use)
2. **Coverage-gap analysis** (small, mostly reporting on existing data)
3. **API + DB generation** (medium, large coverage increase at low runtime cost)
4. **Requirement ingestion at scale** (medium, changes the unit of work)
5. **Visual regression** (medium, catches a class nothing else does)
6. **Test suite health** (medium, keeps the suite trustworthy as it grows)
7. **Appium** (only if mobile matters to you)
8. **Exploratory testing** (large, highest ceiling)

Steps 1–3 are the ones that would most change day-to-day usefulness, and none of
them requires new architecture — only filling in interfaces that already exist.
