"""Test Knowledge Store and QA Knowledge Graph.

Two related jobs, both aimed at the same waste:

**Test Knowledge Store** remembers every scenario the platform has produced —
its feature file, page objects, steps, fixtures, execution history and healing
history. Before designing anything new, the platform asks "have we already done
something like this?". A near-duplicate request is answered by reuse instead of
a fresh (paid) design-and-generate cycle.

**QA Knowledge Graph** records the relationships between a requirement, its
feature, the pages and components it touches, the APIs and tables behind it, and
the tests that cover it. Retrieving that neighbourhood gives an agent exactly the
context it needs, which is the difference between a 2,000-token prompt and a
60,000-token one.

Similarity is computed deterministically (token overlap + shared entities). It
is free, explainable, and good enough to decide whether a paid call is warranted.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select

from packages.aiqa_types.models import new_id
from services.observability.db import session_scope
from services.observability.models import KnowledgeItemRow

log = logging.getLogger("aiqa.testknowledge")

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_STOP = {
    "the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "with", "is", "are",
    "test", "tests", "automate", "automation", "scenario", "case", "should", "verify",
}


def tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOP and len(t) > 2}


def similarity(a: str, b: str) -> float:
    """Blend of containment and Jaccard over significant tokens.

    Plain Jaccard is the wrong metric here: a five-word request compared against
    a stored test's full signature (name + feature + every step) scores near zero
    even when the request is entirely covered by it. Containment — how much of
    the *query* the candidate accounts for — is what we actually care about, with
    Jaccard retained as a tie-breaker so a sprawling candidate does not win by
    sheer size.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    containment = intersection / len(ta)
    jaccard = intersection / len(ta | tb)
    return 0.65 * containment + 0.35 * jaccard


# =========================================================================== #
# Test knowledge
# =========================================================================== #
@dataclass
class TestKnowledge:
    """One remembered scenario and everything attached to it."""

    id: str = field(default_factory=lambda: new_id("tk"))
    project_id: str = ""
    test_id: str = ""
    name: str = ""
    feature: str = ""
    requirement: str = ""
    tags: list[str] = field(default_factory=list)
    #: Acceptance criteria this scenario verifies, stored as text.
    #: Criterion *ids* are regenerated every run, so text is the only identity
    #: that survives long enough to answer "do we already cover this?".
    covers_criteria: list[str] = field(default_factory=list)

    feature_file: str = ""
    step_file: str = ""
    page_objects: list[str] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    apis: list[str] = field(default_factory=list)
    db_tables: list[str] = field(default_factory=list)
    locators: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    runs: int = 0
    passes: int = 0
    failures: int = 0
    heals: int = 0
    last_status: str = ""
    last_run_at: float = 0.0
    created_at: float = field(default_factory=time.time)

    def signature(self) -> str:
        """Text used for similarity matching."""
        return " ".join([self.name, self.feature, self.requirement, " ".join(self.tags), " ".join(self.steps)])

    @property
    def reliability(self) -> float:
        return (self.passes / self.runs) if self.runs else 0.0


class TestKnowledgeStore:
    """Persistence + similarity search over previously generated tests."""

    KIND = "test"

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    # ------------------------------------------------------------------ #
    def put(self, knowledge: TestKnowledge) -> None:
        knowledge.project_id = self.project_id
        payload = asdict(knowledge)
        with session_scope() as session:
            row = session.execute(
                select(KnowledgeItemRow).where(
                    KnowledgeItemRow.project_id == self.project_id,
                    KnowledgeItemRow.kind == self.KIND,
                    KnowledgeItemRow.key == knowledge.test_id,
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    KnowledgeItemRow(
                        id=knowledge.id, project_id=self.project_id, kind=self.KIND,
                        key=knowledge.test_id, name=knowledge.name,
                        payload=payload, signature=knowledge.signature()[:2000],
                    )
                )
            else:
                row.name = knowledge.name
                row.payload = payload
                row.signature = knowledge.signature()[:2000]

    def put_many(self, items: list[TestKnowledge]) -> None:
        for item in items:
            self.put(item)

    def all(self) -> list[TestKnowledge]:
        with session_scope() as session:
            rows = list(
                session.execute(
                    select(KnowledgeItemRow).where(
                        KnowledgeItemRow.project_id == self.project_id,
                        KnowledgeItemRow.kind == self.KIND,
                    )
                ).scalars()
            )
        out: list[TestKnowledge] = []
        for row in rows:
            try:
                known = set(TestKnowledge.__dataclass_fields__)
                out.append(TestKnowledge(**{k: v for k, v in (row.payload or {}).items() if k in known}))
            except Exception:  # noqa: BLE001 - tolerate an older payload shape
                continue
        return out

    def get(self, test_id: str) -> TestKnowledge | None:
        return next((item for item in self.all() if item.test_id == test_id), None)

    # ------------------------------------------------------------------ #
    def find_similar(self, description: str, limit: int = 5, threshold: float = 0.25) -> list[tuple[float, TestKnowledge]]:
        """Rank remembered tests against a new request."""
        scored = [
            (round(similarity(description, item.signature()), 4), item) for item in self.all()
        ]
        scored = [pair for pair in scored if pair[0] >= threshold]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:limit]

    def duplicate_of(self, scenario_name: str, threshold: float = 0.85) -> TestKnowledge | None:
        """Detect that a proposed scenario already exists, before generating it.

        Compares against the scenario *name* rather than the full signature: two
        tests can share a feature, pages and fixtures and still be different
        scenarios, so only near-identical intent counts as a duplicate.
        """
        best: tuple[float, TestKnowledge] | None = None
        for item in self.all():
            score = similarity(scenario_name, item.name)
            if score >= threshold and (best is None or score > best[0]):
                best = (score, item)
        return best[1] if best else None

    def reusable_assets(self, description: str, limit: int = 5) -> dict[str, list[str]]:
        """What existing tests suggest we can reuse for this request."""
        assets: dict[str, list[str]] = {
            "page_objects": [], "fixtures": [], "components": [], "apis": [], "db_tables": [], "routes": [],
        }
        for _score, item in self.find_similar(description, limit=limit):
            for key, values in (
                ("page_objects", item.page_objects),
                ("fixtures", item.fixtures),
                ("components", item.components),
                ("apis", item.apis),
                ("db_tables", item.db_tables),
                ("routes", item.routes),
            ):
                for value in values:
                    if value and value not in assets[key]:
                        assets[key].append(value)
        return assets

    def record_execution(self, test_id: str, status: str, healed: bool = False) -> None:
        knowledge = self.get(test_id)
        if knowledge is None:
            return
        knowledge.runs += 1
        if status == "passed":
            knowledge.passes += 1
        elif status in ("failed", "timed_out"):
            knowledge.failures += 1
        if healed:
            knowledge.heals += 1
        knowledge.last_status = status
        knowledge.last_run_at = time.time()
        self.put(knowledge)

    def stats(self) -> dict[str, Any]:
        items = self.all()
        return {
            "tests_known": len(items),
            "features": len({item.feature for item in items if item.feature}),
            "page_objects": len({po for item in items for po in item.page_objects}),
            "avg_reliability": round(
                sum(item.reliability for item in items) / len(items), 3
            )
            if items
            else 0.0,
            "total_heals": sum(item.heals for item in items),
        }


# =========================================================================== #
# QA Knowledge Graph
# =========================================================================== #
#: Node types, in the order the spec lays them out.
NODE_KINDS = ("requirement", "feature", "page", "component", "api", "db_table", "test", "fixture", "failure")


@dataclass
class GraphNode:
    kind: str
    key: str
    label: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.key}"


@dataclass
class GraphEdge:
    source: str
    target: str
    relation: str = "relates_to"


class QAKnowledgeGraph:
    """Relationship store: requirement -> feature -> page -> component -> api -> db -> test.

    Retrieval walks outward from a seed node so an agent receives the relevant
    neighbourhood — not the whole repository.
    """

    KIND = "graph"
    KEY = "__graph__"

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        with session_scope() as session:
            row = session.execute(
                select(KnowledgeItemRow).where(
                    KnowledgeItemRow.project_id == self.project_id,
                    KnowledgeItemRow.kind == self.KIND,
                    KnowledgeItemRow.key == self.KEY,
                )
            ).scalar_one_or_none()
            payload = (row.payload if row else None) or {}
        for raw in payload.get("nodes", []):
            try:
                node = GraphNode(**raw)
                self.nodes[node.id] = node
            except TypeError:
                continue
        for raw in payload.get("edges", []):
            try:
                self.edges.append(GraphEdge(**raw))
            except TypeError:
                continue

    def save(self) -> None:
        payload = {
            "nodes": [asdict(node) for node in self.nodes.values()],
            "edges": [asdict(edge) for edge in self.edges],
            "saved_at": time.time(),
        }
        with session_scope() as session:
            row = session.execute(
                select(KnowledgeItemRow).where(
                    KnowledgeItemRow.project_id == self.project_id,
                    KnowledgeItemRow.kind == self.KIND,
                    KnowledgeItemRow.key == self.KEY,
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    KnowledgeItemRow(
                        id=new_id("kg"), project_id=self.project_id, kind=self.KIND,
                        key=self.KEY, name="qa-knowledge-graph", payload=payload,
                    )
                )
            else:
                row.payload = payload

    # ------------------------------------------------------------------ #
    def add_node(self, kind: str, key: str, label: str = "", **data: Any) -> GraphNode:
        node = GraphNode(kind=kind, key=key, label=label or key, data=data)
        existing = self.nodes.get(node.id)
        if existing is not None:
            existing.data.update(data)
            if label:
                existing.label = label
            return existing
        self.nodes[node.id] = node
        return node

    def link(self, source: GraphNode | str, target: GraphNode | str, relation: str = "relates_to") -> None:
        source_id = source if isinstance(source, str) else source.id
        target_id = target if isinstance(target, str) else target.id
        if source_id == target_id:
            return
        if not any(e.source == source_id and e.target == target_id and e.relation == relation for e in self.edges):
            self.edges.append(GraphEdge(source_id, target_id, relation))

    # ------------------------------------------------------------------ #
    def neighbours(self, node_id: str, depth: int = 1) -> list[GraphNode]:
        """Breadth-first walk outward — the retrieval primitive."""
        seen = {node_id}
        frontier = [node_id]
        collected: list[GraphNode] = []
        for _ in range(max(1, depth)):
            next_frontier: list[str] = []
            for current in frontier:
                for edge in self.edges:
                    for other in (
                        edge.target if edge.source == current else edge.source if edge.target == current else None,
                    ):
                        if other and other not in seen:
                            seen.add(other)
                            next_frontier.append(other)
                            node = self.nodes.get(other)
                            if node is not None:
                                collected.append(node)
            frontier = next_frontier
            if not frontier:
                break
        return collected

    def context_for(self, description: str, depth: int = 2, limit: int = 40) -> dict[str, list[dict[str, Any]]]:
        """Find the best-matching seed node and return its neighbourhood, grouped by kind."""
        best: tuple[float, GraphNode] | None = None
        for node in self.nodes.values():
            score = similarity(description, f"{node.label} {node.key} {json.dumps(node.data, default=str)[:300]}")
            if score > 0 and (best is None or score > best[0]):
                best = (score, node)
        if best is None:
            return {}

        grouped: dict[str, list[dict[str, Any]]] = {}
        seed = best[1]
        grouped.setdefault(seed.kind, []).append({"key": seed.key, "label": seed.label, **seed.data})
        for node in self.neighbours(seed.id, depth=depth)[:limit]:
            grouped.setdefault(node.kind, []).append({"key": node.key, "label": node.label, **node.data})
        return grouped

    def coverage(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for node in self.nodes.values():
            by_kind[node.kind] = by_kind.get(node.kind, 0) + 1
        requirements = [n for n in self.nodes.values() if n.kind == "requirement"]
        covered = sum(
            1
            for requirement in requirements
            if any(node.kind == "test" for node in self.neighbours(requirement.id, depth=3))
        )
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "by_kind": by_kind,
            "requirements": len(requirements),
            "requirements_covered": covered,
            "coverage_pct": round(100 * covered / len(requirements), 1) if requirements else 0.0,
        }

    # ------------------------------------------------------------------ #
    def ingest_test(self, knowledge: TestKnowledge) -> None:
        """Wire one remembered test into the graph."""
        test_node = self.add_node("test", knowledge.test_id, knowledge.name, tags=knowledge.tags)

        if knowledge.requirement:
            requirement = self.add_node("requirement", knowledge.requirement[:80], knowledge.requirement[:120])
            self.link(requirement, test_node, "verified_by")
        if knowledge.feature:
            feature = self.add_node("feature", knowledge.feature, knowledge.feature, file=knowledge.feature_file)
            self.link(feature, test_node, "contains")
            if knowledge.requirement:
                self.link(f"requirement:{knowledge.requirement[:80]}", feature, "specified_by")

        for page in knowledge.page_objects:
            self.link(self.add_node("page", page, page), test_node, "used_by")
        for component in knowledge.components:
            self.link(self.add_node("component", component, component), test_node, "used_by")
        for api in knowledge.apis:
            self.link(self.add_node("api", api, api), test_node, "exercised_by")
        for table in knowledge.db_tables:
            self.link(self.add_node("db_table", table, table), test_node, "validated_by")
        for fixture in knowledge.fixtures:
            self.link(self.add_node("fixture", fixture, fixture), test_node, "used_by")

    def ingest_failure(self, test_id: str, category: str, summary: str) -> None:
        failure = self.add_node("failure", f"{test_id}:{category}", summary[:120], category=category)
        self.link(f"test:{test_id}", failure, "failed_with")
