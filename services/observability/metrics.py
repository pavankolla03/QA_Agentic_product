"""Metrics — the cost dashboard and the management dashboard.

Two audiences, two questions:

* **A QA lead** asks "what is this costing me and where is it going?" — cost per
  scenario, per model, per agent, free vs paid, cache hit rate.
* **A manager** asks "is it working?" — automation coverage, pass rate, healing
  accuracy, human intervention rate, hours saved.

Everything here is computed from recorded traces. Nothing is estimated except
the hours-saved figure, which is explicitly labelled as an estimate with its
assumption stated, because a fabricated ROI number is worse than none.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select

from configs.settings import load_model_config
from services.observability.db import session_scope
from services.observability.models import (
    AgentTraceRow,
    ApprovalRow,
    CostDailyRow,
    FlakyTestRow,
    HealHistoryRow,
    KnowledgeChunkRow,
    KnowledgeItemRow,
    LLMCallRow,
    ProjectRow,
    RunRow,
)

#: Assumption behind the "hours saved" estimate. Stated, not hidden.
MANUAL_MINUTES_PER_SCENARIO = 45


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# =========================================================================== #
# Cost dashboard
# =========================================================================== #
def cost_dashboard(org_id: str = "", days: int = 30) -> dict[str, Any]:
    """Where the money went, at the granularity the spec asks for."""
    cutoff = _since(days)

    with session_scope() as session:
        run_filter = [RunRow.created_at >= cutoff]
        if org_id:
            run_filter.append(RunRow.org_id == org_id)
        runs = list(session.execute(select(RunRow).where(*run_filter)).scalars())
        run_ids = [r.id for r in runs]

        calls = (
            list(session.execute(select(LLMCallRow).where(LLMCallRow.run_id.in_(run_ids))).scalars())
            if run_ids
            else []
        )
        daily = list(
            session.execute(
                select(CostDailyRow).where(CostDailyRow.day >= cutoff.strftime("%Y-%m-%d"))
            ).scalars()
        )

    scenarios = sum(
        sum(len(f.get("scenarios", [])) for f in (r.test_plan or {}).get("features", [])) for r in runs
    )
    successful = [r for r in runs if r.status == "succeeded"]
    total_cost = sum(r.total_cost_usd for r in runs)

    by_model: dict[str, dict[str, Any]] = {}
    by_provider: dict[str, dict[str, Any]] = {}
    by_agent: dict[str, dict[str, Any]] = {}
    by_tier: dict[str, dict[str, Any]] = {}
    free_calls = paid_calls = 0
    cached_tokens = input_tokens = output_tokens = 0

    for call in calls:
        cost = float(call.cost_usd or 0)
        tokens = int(call.total_tokens or 0)
        input_tokens += int(call.prompt_tokens or 0)
        output_tokens += int(call.completion_tokens or 0)
        cached_tokens += int(call.cached_tokens or 0)
        if cost > 0:
            paid_calls += 1
        else:
            free_calls += 1

        for bucket, key in (
            (by_model, call.model or "unknown"),
            (by_provider, call.provider or "unknown"),
            (by_agent, call.agent or "unknown"),
            (by_tier, call.capability or "unknown"),
        ):
            entry = bucket.setdefault(key, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
            entry["calls"] += 1
            entry["tokens"] += tokens
            entry["cost_usd"] = round(entry["cost_usd"] + cost, 8)

    # The saving the architecture is responsible for: context never sent.
    tokens_saved = sum(
        int((r.metadata_json or {}).get("budget_consumed", {}).get("tokens_saved", 0)) for r in runs
    )

    baseline = load_model_config().get("baseline") or {}
    baseline_per_test = float(baseline.get("credits_per_test", 273) or 273)

    return {
        "window_days": days,
        "totals": {
            "runs": len(runs),
            "scenarios": scenarios,
            "llm_requests": len(calls),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "tokens_saved_by_cache": tokens_saved,
            "free_model_calls": free_calls,
            "paid_model_calls": paid_calls,
            "free_call_share_pct": round(100 * free_calls / len(calls), 1) if calls else 0.0,
            "total_cost_usd": round(total_cost, 6),
        },
        "unit_economics": {
            "cost_per_scenario_usd": round(total_cost / scenarios, 6) if scenarios else 0.0,
            "cost_per_run_usd": round(total_cost / len(runs), 6) if runs else 0.0,
            "cost_per_successful_automation_usd": round(
                sum(r.total_cost_usd for r in successful) / len(successful), 6
            )
            if successful
            else 0.0,
            "tokens_per_scenario": round((input_tokens + output_tokens) / scenarios) if scenarios else 0,
            "requests_per_scenario": round(len(calls) / scenarios, 2) if scenarios else 0.0,
        },
        "baseline_comparison": {
            "baseline_credits_per_test": baseline_per_test,
            "baseline_source": "user-supplied benchmark (30,000 credits / 110 tests)",
            "measured_requests_per_scenario": round(len(calls) / scenarios, 2) if scenarios else 0.0,
            "note": (
                "Credits and dollars are not directly comparable across providers. "
                "Compare requests and tokens per scenario, and run scripts/benchmark.py --live "
                "for a dollar figure on your own provider mix."
            ),
        },
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["cost_usd"])),
        "by_provider": by_provider,
        "by_agent": dict(sorted(by_agent.items(), key=lambda kv: -kv[1]["cost_usd"])),
        "by_tier": by_tier,
        "daily": _daily_series(daily, org_id),
        "retries": sum(1 for c in calls if c.status == "failed"),
        "fallbacks": sum(1 for c in calls if c.fallback_from),
    }


def _daily_series(rows: list[CostDailyRow], org_id: str) -> list[dict[str, Any]]:
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        if org_id and row.org_id and row.org_id != org_id:
            continue
        entry = by_day.setdefault(row.day, {"day": row.day, "cost_usd": 0.0, "tokens": 0, "calls": 0})
        entry["cost_usd"] = round(entry["cost_usd"] + row.cost_usd, 6)
        entry["tokens"] += row.total_tokens
        entry["calls"] += row.calls
    return [by_day[day] for day in sorted(by_day)]


# =========================================================================== #
# Management dashboard
# =========================================================================== #
def management_dashboard(org_id: str = "", days: int = 30) -> dict[str, Any]:
    """The view a QA manager needs: is this working, and by how much?"""
    cutoff = _since(days)

    with session_scope() as session:
        run_filter = [RunRow.created_at >= cutoff]
        if org_id:
            run_filter.append(RunRow.org_id == org_id)
        runs = list(session.execute(select(RunRow).where(*run_filter)).scalars())
        run_ids = [r.id for r in runs]

        projects = list(
            session.execute(
                select(ProjectRow).where(ProjectRow.org_id == org_id) if org_id else select(ProjectRow)
            ).scalars()
        )
        heals = list(session.execute(select(HealHistoryRow)).scalars())
        approvals = (
            list(session.execute(select(ApprovalRow).where(ApprovalRow.run_id.in_(run_ids))).scalars())
            if run_ids
            else []
        )
        flaky = list(
            session.execute(select(FlakyTestRow).order_by(FlakyTestRow.flakes.desc()).limit(20)).scalars()
        )
        known_tests = int(
            session.execute(
                select(func.count()).select_from(KnowledgeItemRow).where(KnowledgeItemRow.kind == "test")
            ).scalar_one()
            or 0
        )
        indexed_projects = {
            pid for (pid,) in session.execute(select(KnowledgeChunkRow.project_id).distinct()).all()
        }
        agent_stats = session.execute(
            select(
                AgentTraceRow.agent,
                func.count().label("runs"),
                func.sum(case((AgentTraceRow.status == "failed", 1), else_=0)).label("failures"),
                func.avg(AgentTraceRow.latency_ms).label("latency"),
            ).group_by(AgentTraceRow.agent)
        ).all()

    scenarios = sum(
        sum(len(f.get("scenarios", [])) for f in (r.test_plan or {}).get("features", [])) for r in runs
    )
    tests_total = sum(r.tests_total for r in runs)
    tests_passed = sum(r.tests_passed for r in runs)
    executed_runs = [r for r in runs if r.tests_total]

    verified_heals = sum(1 for h in heals if h.verified)
    reverted_heals = sum(1 for h in heals if h.reverted)
    applied_heals = sum(1 for h in heals if h.applied)

    human_responses = [a for a in approvals if a.status in ("approved", "rejected", "changes_requested")]
    rejected = sum(1 for a in approvals if a.status in ("rejected", "changes_requested"))

    # Acceptance rate: a rejected gate means the generated work was not good enough.
    acceptance_rate = (
        round(100 * (len(human_responses) - rejected) / len(human_responses), 1) if human_responses else None
    )

    return {
        "window_days": days,
        "projects": {
            "total": len(projects),
            "indexed": len([p for p in projects if p.id in indexed_projects]),
        },
        "automation": {
            "runs": len(runs),
            "scenarios_generated": scenarios,
            "tests_known": known_tests,
            "files_generated": sum(r.files_changed for r in runs),
            "run_success_rate_pct": round(
                100 * sum(1 for r in runs if r.status == "succeeded") / len(runs), 1
            )
            if runs
            else 0.0,
            "generated_code_acceptance_rate_pct": acceptance_rate,
            "acceptance_rate_note": (
                "share of human approval gates accepted without rejection or changes requested"
                if acceptance_rate is not None
                else "no human gates have been answered yet"
            ),
        },
        "execution": {
            "tests_executed": tests_total,
            "tests_passed": tests_passed,
            "pass_rate_pct": round(100 * tests_passed / tests_total, 1) if tests_total else 0.0,
            "runs_that_executed": len(executed_runs),
            "avg_duration_s": round(sum(r.duration_s for r in runs) / len(runs), 1) if runs else 0.0,
        },
        "self_healing": {
            "proposed": len(heals),
            "applied": applied_heals,
            "verified": verified_heals,
            "reverted": reverted_heals,
            "success_rate_pct": round(100 * verified_heals / applied_heals, 1) if applied_heals else 0.0,
            "false_heal_rate_pct": round(100 * reverted_heals / applied_heals, 1) if applied_heals else 0.0,
            "false_heal_note": "repairs that were applied, failed verification and were rolled back",
        },
        "human_involvement": {
            "approval_gates_raised": len(approvals),
            "gates_answered": len(human_responses),
            "gates_rejected": rejected,
            "intervention_rate_pct": round(100 * len(approvals) / len(runs), 1) if runs else 0.0,
            "intervention_note": "approval gates raised per run; lower means more autonomy earned",
        },
        "agents": [
            {
                "agent": agent,
                "invocations": int(count or 0),
                "failures": int(failures or 0),
                "success_rate_pct": round(100 * (1 - (failures or 0) / count), 1) if count else 0.0,
                "avg_latency_ms": int(latency or 0),
            }
            for agent, count, failures, latency in agent_stats
        ],
        "flaky_tests": [
            {
                "test_id": f.test_id, "name": f.test_name, "file": f.file_path,
                "runs": f.runs, "flakes": f.flakes, "failures": f.failures,
                "flake_rate_pct": round(100 * f.flakes / f.runs, 1) if f.runs else 0.0,
                "quarantined": f.quarantined,
                "recommendation": "quarantine" if f.runs >= 5 and f.flakes / f.runs > 0.3 else "monitor",
            }
            for f in flaky
            if f.flakes
        ],
        "estimated_impact": {
            "manual_minutes_per_scenario": MANUAL_MINUTES_PER_SCENARIO,
            "hours_saved_estimate": round(scenarios * MANUAL_MINUTES_PER_SCENARIO / 60, 1),
            "assumption": (
                f"Assumes {MANUAL_MINUTES_PER_SCENARIO} minutes to hand-write one scenario including "
                f"page objects and steps. This is an estimate, not a measurement - change the constant "
                f"to match your team's actual throughput."
            ),
        },
    }


# =========================================================================== #
def savings_report(org_id: str = "", days: int = 30) -> dict[str, Any]:
    """What the knowledge layer actually avoided. Measured, not projected."""
    cutoff = _since(days)
    with session_scope() as session:
        run_filter = [RunRow.created_at >= cutoff]
        if org_id:
            run_filter.append(RunRow.org_id == org_id)
        runs = list(session.execute(select(RunRow).where(*run_filter)).scalars())

    cache_hits = 0
    appmap_hits = 0
    duplicates = 0
    tokens_saved = 0
    for run in runs:
        metadata = run.metadata_json or {}
        delta = metadata.get("index_delta") or {}
        if delta.get("reused_from_cache"):
            cache_hits += 1
        notes = " ".join(metadata.get("notes", []))
        if "application map hit" in notes or "no crawl needed" in notes:
            appmap_hits += 1
        duplicates += len(metadata.get("duplicates_dropped", []) or [])
        tokens_saved += int((metadata.get("budget_consumed") or {}).get("tokens_saved", 0))

    return {
        "runs": len(runs),
        "repository_index_cache_hits": cache_hits,
        "repository_cache_hit_rate_pct": round(100 * cache_hits / len(runs), 1) if runs else 0.0,
        "application_map_hits": appmap_hits,
        "application_map_hit_rate_pct": round(100 * appmap_hits / len(runs), 1) if runs else 0.0,
        "duplicate_scenarios_avoided": duplicates,
        "context_tokens_avoided": tokens_saved,
        "interpretation": (
            "Cache hit rates and avoided tokens are measured directly. They are the mechanism behind "
            "the cost reduction; the dollar figure depends on your provider mix."
        ),
    }
