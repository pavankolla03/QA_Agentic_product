"""Cost benchmark — measure the savings instead of claiming them.

The user-supplied baseline is **~30,000 AI credits for 110 test cases**
(≈273 credits/test) on a naive, re-discover-everything architecture. This script
measures what this platform actually consumes for the same shape of work, and
reports the delta.

It runs the same project several times so the *knowledge effect* is visible: the
first run pays to index the repository, crawl the application and design from
scratch; later runs reuse the repository map, the application map and the test
knowledge store. If the architecture works, cost per scenario falls sharply from
run 1 to run 2 and stays low.

    python -m scripts.benchmark                # offline (deterministic, free)
    python -m scripts.benchmark --live         # use configured providers
    python -m scripts.benchmark --runs 5

Offline mode measures *request and token counts*, which is what the
architecture controls. Dollar figures are meaningful only with `--live`, and are
labelled as such.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

#: The instructions a QA engineer would realistically issue against one app.
WORKLOAD = [
    "Automate the Resident Registration functionality",
    "Automate resident search and filtering",
    "Automate editing an existing resident record",
    "Add negative and boundary coverage to resident registration",
    "Automate the resident deletion flow with confirmation",
    "Automate resident registration validation messages",
]


@dataclass
class RunMetrics:
    index: int
    instruction: str
    scenarios: int = 0
    files: int = 0
    llm_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    tokens_saved: int = 0
    cost_usd: float = 0.0
    free_calls: int = 0
    paid_calls: int = 0
    duration_s: float = 0.0
    by_tier: dict[str, int] = field(default_factory=dict)
    reused_index: bool = False
    reused_appmap: bool = False
    #: False when the run answered from the Test Knowledge Store instead of
    #: paying for the design call. The single largest per-run saving.
    designed_this_run: bool = True
    duplicates_dropped: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tokens_per_scenario(self) -> float:
        return self.total_tokens / self.scenarios if self.scenarios else 0.0

    @property
    def requests_per_scenario(self) -> float:
        return self.llm_requests / self.scenarios if self.scenarios else 0.0


def _banner(text: str) -> None:
    print(f"\n{'=' * 92}\n{text}\n{'=' * 92}")


async def benchmark(runs: int, live: bool, keep: bool) -> int:
    workspace = Path(tempfile.mkdtemp(prefix="aiqa-bench-"))
    os.environ["AIQA_DATABASE_URL"] = f"sqlite:///{(workspace / 'bench.db').as_posix()}"
    os.environ["AIQA_ARTIFACTS_DIR"] = str(workspace / "artifacts")
    os.environ["AIQA_PER_RUN_COST_LIMIT_USD"] = "5.0"
    os.environ["AIQA_DAILY_COST_LIMIT_USD"] = "1000"

    from configs.settings import load_model_config, reset_config_cache

    reset_config_cache()

    from packages.aiqa_types.enums import RunMode
    from packages.aiqa_types.models import RunRequest, new_id
    from services.agent_engine.engine import AgentEngine
    from services.observability.db import init_db, reset_db_state, session_scope
    from services.observability.models import LLMCallRow, OrgRow, ProjectRow, RunRow, UserRow

    reset_db_state()
    init_db()

    # ---- a realistic target repository, under Git ---------------------- #
    project_root = workspace / "acme-web-e2e"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "sample-repo", project_root)
    for command in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-q", "-m", "init"]):
        subprocess.run(command, cwd=project_root, check=False, capture_output=True)

    org_id, user_id, project_id = new_id("org"), new_id("usr"), new_id("prj")
    with session_scope() as session:
        session.add(OrgRow(id=org_id, name="Benchmark Org"))
        session.add(UserRow(id=user_id, org_id=org_id, email="bench@test.local", role="lead"))
        session.add(
            ProjectRow(
                id=project_id, org_id=org_id, name="acme-web-e2e",
                repository_path=str(project_root),
                base_url="http://127.0.0.1:59999",
                per_run_cost_limit_usd=5.0,
            )
        )

    # A real application under test, so exploration and the application map are
    # exercised rather than short-circuited by an unreachable host.
    from scripts.demo_app import DemoApp

    app = DemoApp()
    app.__enter__()
    with session_scope() as session:
        row = session.get(ProjectRow, project_id)
        row.base_url = app.base_url
    print(f"  app under test: {app.base_url}")

    engine = AgentEngine(offline=not live)
    metrics: list[RunMetrics] = []

    _banner(f"Cost benchmark - {runs} run(s), {'LIVE providers' if live else 'offline deterministic mode'}")
    print(f"  workspace: {project_root}")
    print(f"  workload:  {len(WORKLOAD)} instruction(s), cycled\n")
    print(f"  {'#':<3} {'scenarios':>9} {'files':>6} {'reqs':>5} {'in tok':>9} {'out tok':>8} "
          f"{'saved':>9} {'cost $':>9} {'sec':>6}  reuse")
    print(f"  {'-' * 88}")

    for index in range(1, runs + 1):
        instruction = WORKLOAD[(index - 1) % len(WORKLOAD)]
        started = time.perf_counter()

        run_id = engine.create_run(
            RunRequest(
                project_id=project_id, instruction=instruction,
                mode=RunMode.GENERATE, auto_approve=True, max_cost_usd=5.0,
            ),
            user_id=user_id, org_id=org_id,
        )
        result = await engine.run_to_completion(run_id, auto_approve=True)
        elapsed = time.perf_counter() - started

        with session_scope() as session:
            row = session.get(RunRow, run_id)
            calls = list(
                session.execute(LLMCallRow.__table__.select().where(LLMCallRow.run_id == run_id))
            )
            metadata = dict(row.metadata_json or {})
            consumed = metadata.get("budget_consumed", {}) or {}
            plan = row.test_plan or {}

            measurement = RunMetrics(
                index=index,
                instruction=instruction,
                scenarios=sum(len(f.get("scenarios", [])) for f in plan.get("features", [])),
                files=row.files_changed,
                llm_requests=len(calls),
                input_tokens=int(consumed.get("input_tokens", row.prompt_tokens)),
                output_tokens=int(consumed.get("output_tokens", row.completion_tokens)),
                cached_tokens=int(consumed.get("cached_tokens", 0)),
                tokens_saved=int(consumed.get("tokens_saved", 0)),
                cost_usd=float(row.total_cost_usd),
                free_calls=int(consumed.get("free_calls", 0)),
                paid_calls=int(consumed.get("paid_calls", 0)),
                duration_s=elapsed,
                duplicates_dropped=len(metadata.get("duplicates_dropped", []) or []),
            )
            notes = " ".join(metadata.get("notes", []))
            measurement.reused_index = "reused cached map" in notes or "unchanged" in notes
            measurement.reused_appmap = "application map hit" in notes or "no crawl needed" in notes
            measurement.designed_this_run = "design skipped" not in notes

        metrics.append(measurement)
        reuse_flags = "".join(
            [
                "idx " if measurement.reused_index else "",
                "app " if measurement.reused_appmap else "",
                "" if measurement.designed_this_run else "no-design ",
                f"dup-{measurement.duplicates_dropped}" if measurement.duplicates_dropped else "",
            ]
        ) or "-"
        print(
            f"  {index:<3} {measurement.scenarios:>9} {measurement.files:>6} {measurement.llm_requests:>5} "
            f"{measurement.input_tokens:>9,} {measurement.output_tokens:>8,} "
            f"{measurement.tokens_saved:>9,} {measurement.cost_usd:>9.5f} {elapsed:>6.1f}  {reuse_flags}"
        )
        if result.status.value not in ("succeeded", "waiting_approval"):
            print(f"      ! run {index} ended as {result.status.value}: {result.error[:90]}")

    # ---- analysis ------------------------------------------------------ #
    _banner("Knowledge effect (does it get cheaper with use?)")
    first, later = metrics[0], metrics[1:]
    if later:
        avg_later_tokens = sum(m.total_tokens for m in later) / len(later)
        avg_later_reqs = sum(m.llm_requests for m in later) / len(later)
        token_drop = (1 - avg_later_tokens / first.total_tokens) * 100 if first.total_tokens else 0.0
        req_drop = (1 - avg_later_reqs / first.llm_requests) * 100 if first.llm_requests else 0.0

        print(f"  run 1 (cold):        {first.total_tokens:>9,} tokens   {first.llm_requests:>3} requests")
        print(f"  runs 2+ (warm, avg): {avg_later_tokens:>9,.0f} tokens   {avg_later_reqs:>3.1f} requests")
        print(f"  per-run reduction:   {token_drop:>9.1f}% tokens  {req_drop:>5.1f}% requests")
        print()
        # Runs differ in how many NEW scenarios they produce (dedupe removes the
        # rest), so per-run totals alone are misleading. Work actually avoided is
        # the honest measure.
        total_designed = sum(m.scenarios for m in metrics)
        total_dropped = sum(m.duplicates_dropped for m in metrics)
        skipped_designs = sum(1 for m in later if not getattr(m, "designed_this_run", True))
        attempted = total_designed + total_dropped
        print(f"  scenarios designed:  {total_designed}")
        print(f"  duplicates avoided:  {total_dropped} of {attempted} attempted "
              f"({100 * total_dropped / attempted if attempted else 0:.0f}% of design work skipped)")
        warm_scenarios = sum(m.scenarios for m in later)
        print(f"  design calls skipped:   {skipped_designs}/{len(later)} warm runs "
              f"(the requirement was already covered)")
        if warm_scenarios:
            print(f"  tokens per NEW scenario: run 1 {first.tokens_per_scenario:,.0f}  |  "
                  f"warm avg {sum(m.total_tokens for m in later) / warm_scenarios:,.0f}")
        else:
            # Dividing by zero new scenarios produced a meaningless number that
            # looked like a catastrophic regression. Say what happened instead.
            print("  tokens per NEW scenario: warm runs designed nothing new — every")
            print("                           requirement was already covered.")
        if not live:
            print()
            print("  NOTE on the warm figures in offline mode:")
            print("   The deterministic provider emits a fixed scenario template, so when a warm run")
            print("   DOES reach the design call it pays full price and then discards most of the")
            print("   result as duplicates. A real model honours the 'do not redesign what already")
            print("   exists' instruction, so the warm token figure understates the architecture.")
            print("   Runs where design was skipped entirely are not affected by this: that decision")
            print("   is deterministic, and so are the caching numbers below. Those are the")
            print("   trustworthy signals in offline mode.")
        print()
        print(f"  index cache hits:       {sum(1 for m in later if m.reused_index)}/{len(later)} warm runs")
        print(f"  application map hits:   {sum(1 for m in later if m.reused_appmap)}/{len(later)} warm runs")
        print(f"  duplicate scenarios dropped: {sum(m.duplicates_dropped for m in metrics)}")
        print(f"  context avoided (tokens not sent): {sum(m.tokens_saved for m in metrics):,}")
    else:
        print("  (need at least 2 runs to measure the knowledge effect)")

    _banner("Model routing")
    with session_scope() as session:
        all_calls = list(session.execute(LLMCallRow.__table__.select()))
    by_tier: dict[str, int] = {}
    by_model: dict[str, int] = {}
    paid_tokens = free_tokens = 0
    for call in all_calls:
        tier = str(call.capability)
        by_tier[tier] = by_tier.get(tier, 0) + 1
        by_model[f"{call.provider}/{call.model}"] = by_model.get(f"{call.provider}/{call.model}", 0) + 1
        if float(call.cost_usd or 0) > 0:
            paid_tokens += int(call.total_tokens or 0)
        else:
            free_tokens += int(call.total_tokens or 0)

    total_calls = len(all_calls) or 1
    for tier, count in sorted(by_tier.items(), key=lambda kv: -kv[1]):
        print(f"  {tier:<12} {count:>4} call(s)  {100 * count / total_calls:>5.1f}%")
    print()
    for model, count in sorted(by_model.items(), key=lambda kv: -kv[1])[:6]:
        print(f"    {model:<52} {count:>4}")
    print()
    print(f"  tokens on free/local models: {free_tokens:,}")
    print(f"  tokens on paid models:       {paid_tokens:,}")
    if free_tokens + paid_tokens:
        print(f"  share of tokens that were free: {100 * free_tokens / (free_tokens + paid_tokens):.1f}%")

    # ---- versus the stated baseline ------------------------------------ #
    _banner("Versus the stated baseline")
    baseline = (load_model_config().get("baseline") or {})
    baseline_per_test = float(baseline.get("credits_per_test", 273))
    total_scenarios = sum(m.scenarios for m in metrics)
    total_tokens = sum(m.total_tokens for m in metrics)
    total_requests = sum(m.llm_requests for m in metrics)
    total_cost = sum(m.cost_usd for m in metrics)

    print(f"  baseline:  {baseline.get('credits_total', 30000):,} credits / "
          f"{baseline.get('test_cases', 110)} tests = {baseline_per_test:.0f} credits per test")
    print()
    print(f"  measured:  {total_scenarios} scenario(s) across {len(metrics)} run(s)")
    print(f"             {total_requests} LLM request(s)  ->  "
          f"{total_requests / total_scenarios if total_scenarios else 0:.2f} per scenario")
    print(f"             {total_tokens:,} token(s)        ->  "
          f"{total_tokens / total_scenarios if total_scenarios else 0:,.0f} per scenario")
    if live:
        print(f"             ${total_cost:.4f}                ->  "
              f"${total_cost / total_scenarios if total_scenarios else 0:.5f} per scenario")
        projected = (total_cost / total_scenarios * 110) if total_scenarios else 0
        print(f"\n  projected cost for a 110-test suite: ${projected:.2f}")
    else:
        print("             cost: $0.0000 (offline deterministic provider - not a price signal)")
        print("\n  Run with --live and real providers configured to measure dollars.")
        print("  What offline mode does prove: request count, token volume and cache hit rate,")
        print("  which are the quantities the architecture actually controls.")

    print()
    print("  Honest reading of these numbers:")
    print("   - request/token counts are real and comparable across runs")
    print("   - the run-1 vs run-2+ delta is the knowledge effect, and is the headline claim")
    print("   - absolute dollars require --live; nothing here should be quoted as a $ saving")

    if keep:
        print(f"\n  workspace kept at: {workspace}")
    else:
        reset_db_state()
        shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI QA Engineer cost benchmark")
    parser.add_argument("--runs", type=int, default=4, help="how many runs to measure")
    parser.add_argument("--live", action="store_true", help="use configured providers instead of offline mode")
    parser.add_argument("--keep", action="store_true", help="keep the temporary workspace")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(benchmark(runs=args.runs, live=args.live, keep=args.keep)))
