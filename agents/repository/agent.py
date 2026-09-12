"""Repository Understanding Agent.

Before generating a single line, the platform learns how *this* team writes
tests: the directory layout, the base classes, the fixtures that already exist,
the naming conventions, the test-id scheme. This is what separates generated
code that gets merged from generated code that gets rewritten by hand.

The structural work here is deterministic (parsers, not prompts). The LLM is
used only for the one thing it is better at: writing the prose summary of
conventions that later prompts consume.
"""

from __future__ import annotations

from typing import Any

from agents.base import AgentContext, BaseAgent
from packages.aiqa_types.enums import AgentName, Capability
from services.knowledge_service.indexer import (
    KnowledgeRetriever,
    RepositoryIndexer,
    summarize_conventions,
)
from tools.shell.shell_tools import probe_toolchain

SYSTEM = """You are a code-comprehension specialist for QA automation repositories.
You are given hard facts extracted from a repository by static analysis.
Write a concise briefing (max 12 short lines) telling a code-generating agent exactly how to write
new tests so they are indistinguishable from the existing ones.

Cover only what the facts support: where files go, what to extend, which fixtures/utilities to reuse
instead of re-creating, naming and test-id conventions, and locator discipline.
Do not invent files or APIs that are not in the facts. Plain prose, no JSON, no preamble."""


class RepositoryAgent(BaseAgent):
    name = AgentName.REPOSITORY
    capability = Capability.FAST
    description = "Indexes the target repository and learns its conventions, layout and reusable assets."

    def progress(self, ctx: AgentContext) -> float:
        return 0.18

    async def run(self, ctx: AgentContext) -> None:
        ctx.toolchain = probe_toolchain(ctx.project_root)

        indexer = RepositoryIndexer(ctx.project.id, ctx.project_root)
        profile = await indexer.index(router=ctx.router)
        profile.project_id = ctx.project.id
        ctx.repo_profile = profile

        if profile.file_count == 0:
            ctx.warn(
                "no indexable source files found in the repository — generated tests will follow the "
                "organization standard defaults instead of learned conventions"
            )
        elif profile.test_runner == "unknown":
            ctx.warn(
                "could not identify a test runner. Generation will target Playwright per the "
                "organization standard; verify the output matches your framework."
            )

        # Retrieve the existing code most relevant to this instruction so later
        # agents see real examples rather than abstractions.
        query_parts = [ctx.instruction]
        if ctx.requirement:
            query_parts.append(ctx.requirement.title)
            query_parts.extend(c.text for c in ctx.requirement.acceptance_criteria[:5])
        retriever = KnowledgeRetriever(ctx.project.id)
        hits = await retriever.search(" ".join(query_parts), router=ctx.router, limit=8)
        ctx.retrieved_context = retriever.context_block(hits, max_chars=10000)
        ctx.metadata["retrieved_files"] = [h["file_path"] for h in hits]

        # Let the model phrase the briefing; fall back to the deterministic one.
        deterministic = profile.conventions_summary or summarize_conventions(profile, [])
        if profile.file_count:
            facts = _facts_block(profile)
            response = await self.ask(
                ctx, SYSTEM, facts, task="repository.summarize", max_tokens=700, temperature=0.0
            )
            briefing = (response.text or "").strip()
            if len(briefing) > 80:
                profile.conventions_summary = briefing + "\n\n[Verified facts]\n" + deterministic
            else:
                profile.conventions_summary = deterministic

        pages = len(profile.symbols_of("page_object"))
        fixtures = len(profile.symbols_of("fixture"))
        ctx.note(
            f"indexed {profile.file_count} files, {len(profile.symbols)} symbols "
            f"({pages} page objects, {fixtures} fixtures), {profile.indexed_chunks} chunks; "
            f"runner={profile.test_runner}, bdd={profile.bdd}"
        )
        if ctx.tracker:
            for trace in getattr(ctx.tracker, "agent_traces", []):
                if trace.agent == self.name and not trace.output_summary:
                    trace.output_summary = (
                        f"{profile.file_count} files, {len(profile.symbols)} symbols, "
                        f"layout={len(profile.detected_layout)} dirs"
                    )


def _facts_block(profile: Any) -> str:
    pages = profile.symbols_of("page_object")
    fixtures = profile.symbols_of("fixture")
    utils = profile.symbols_of("util")
    steps = profile.symbols_of("step")

    lines = [
        "REPOSITORY FACTS (from static analysis — treat as ground truth):",
        f"- language: {profile.language}; test runner: {profile.test_runner}; BDD: {profile.bdd}; "
        f"package manager: {profile.package_manager}",
        f"- frameworks: {', '.join(profile.frameworks) or 'none detected'}",
        f"- config files: {', '.join(profile.config_files) or 'none'}",
        "- directory layout: "
        + (", ".join(f"{k}={v}" for k, v in sorted(profile.detected_layout.items())) or "not detected"),
        "- naming conventions: "
        + (", ".join(f"{k}={v}" for k, v in sorted(profile.naming_conventions.items())) or "not detected"),
    ]
    if pages:
        lines.append("- existing page objects:")
        for symbol in pages[:12]:
            members = ", ".join(symbol.members[:8]) or "no public methods detected"
            lines.append(f"    * {symbol.name} ({symbol.file_path}) — {symbol.signature}; methods: {members}")
    if fixtures:
        lines.append(
            "- existing fixtures: " + ", ".join(f"{f.name} ({f.file_path})" for f in fixtures[:12])
        )
    if utils:
        lines.append("- existing utilities: " + ", ".join(sorted({f"{u.name}" for u in utils})[:15]))
    if steps:
        sample = [s.signature or s.name for s in steps[:10]]
        lines.append("- existing step definitions / scenarios: " + " | ".join(s[:90] for s in sample))
    if profile.existing_features:
        lines.append("- existing feature files cover: " + ", ".join(profile.existing_features[:12]))
    return "\n".join(lines)
