"""Coverage-gap analysis — what the suite does *not* cover.

Every other part of the platform answers "automate this". This one answers the
question a QA lead actually has to defend in a release meeting: *what did we
not test?*

It is a pure join over knowledge the platform already holds, so it costs no
model call and can run on every commit:

* the **Application Map** knows every route, component and endpoint that was
  observed;
* the **Test Knowledge Store** knows which routes, pages, components and
  endpoints each remembered test touches;
* the **QA Knowledge Graph** knows which requirements have a test at all.

Anything present in the first and absent from the others is a gap. Each gap
carries a ready-to-run instruction, because a coverage report nobody can act on
is just a list of reasons to feel bad.

Two deliberate limits, so the numbers are not oversold:

* A route being *touched* by a test is not proof it is *well* tested. The report
  says "no test touches this", never "this is tested".
* Only observed surface counts. A page exploration never reached is reported as
  unexplored, not as covered.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.knowledge_service.application_map import ApplicationMap
    from services.knowledge_service.test_knowledge import QAKnowledgeGraph, TestKnowledgeStore


#: Ordered worst-first, which is also the order the report presents them in.
SEVERITY_ORDER = ("high", "medium", "low")


@dataclass
class CoverageGap:
    """One thing that is not covered, and what to do about it."""

    kind: str                       # route | endpoint | component | requirement | negative
    key: str
    label: str
    severity: str = "medium"
    reason: str = ""
    suggested_instruction: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CoverageReport:
    routes_total: int = 0
    routes_covered: int = 0
    endpoints_total: int = 0
    endpoints_covered: int = 0
    components_total: int = 0
    components_covered: int = 0
    requirements_total: int = 0
    requirements_covered: int = 0
    tests_total: int = 0
    gaps: list[CoverageGap] = field(default_factory=list)

    @property
    def route_pct(self) -> float:
        return _pct(self.routes_covered, self.routes_total)

    @property
    def endpoint_pct(self) -> float:
        return _pct(self.endpoints_covered, self.endpoints_total)

    @property
    def requirement_pct(self) -> float:
        return _pct(self.requirements_covered, self.requirements_total)

    def by_severity(self, severity: str) -> list[CoverageGap]:
        return [gap for gap in self.gaps if gap.severity == severity]

    def to_dict(self) -> dict[str, Any]:
        return {
            "routes": {"total": self.routes_total, "covered": self.routes_covered, "pct": self.route_pct},
            "endpoints": {
                "total": self.endpoints_total,
                "covered": self.endpoints_covered,
                "pct": self.endpoint_pct,
            },
            "components": {"total": self.components_total, "covered": self.components_covered},
            "requirements": {
                "total": self.requirements_total,
                "covered": self.requirements_covered,
                "pct": self.requirement_pct,
            },
            "tests": self.tests_total,
            "gap_count": len(self.gaps),
            "gaps": [gap.to_dict() for gap in self.gaps],
        }

    def summary(self) -> str:
        if not self.gaps:
            return (
                f"No coverage gaps found across {self.routes_total} route(s), "
                f"{self.endpoints_total} endpoint(s) and {self.requirements_total} requirement(s)."
            )
        high = len(self.by_severity("high"))
        return (
            f"{len(self.gaps)} coverage gap(s), {high} high severity. "
            f"Routes {self.route_pct}% touched, endpoints {self.endpoint_pct}%, "
            f"requirements {self.requirement_pct}%."
        )


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


# --------------------------------------------------------------------------- #
def analyse_coverage(
    application_map: ApplicationMap | None,
    store: TestKnowledgeStore | None,
    graph: QAKnowledgeGraph | None = None,
    *,
    max_gaps: int = 60,
) -> CoverageReport:
    """Join what exists against what is tested."""
    report = CoverageReport()
    tests = store.all() if store is not None else []
    report.tests_total = len(tests)

    tested_routes = {_normalise_route(r) for test in tests for r in test.routes if r}
    tested_apis = {_normalise_api(a) for test in tests for a in test.apis if a}
    tested_components = {str(c).strip().lower() for test in tests for c in test.components if c}

    gaps: list[CoverageGap] = []

    # ---- routes ---------------------------------------------------------- #
    if application_map is not None:
        routes = sorted(application_map.pages)
        report.routes_total = len(routes)
        for route in routes:
            if _normalise_route(route) in tested_routes:
                report.routes_covered += 1
                continue
            gaps.append(
                CoverageGap(
                    kind="route",
                    key=route,
                    label=route,
                    severity=_route_severity(application_map, route),
                    reason="no remembered test touches this route",
                    suggested_instruction=f"Automate the main flows on {route}",
                )
            )

        # ---- endpoints --------------------------------------------------- #
        endpoints = application_map.api_catalog()
        report.endpoints_total = len(endpoints)
        for endpoint in endpoints:
            key = f"{endpoint['method']} {endpoint['path']}"
            if _normalise_api(key) in tested_apis:
                report.endpoints_covered += 1
                continue
            gaps.append(
                CoverageGap(
                    kind="endpoint",
                    key=key,
                    label=key,
                    # A write endpoint with no test is the one that can corrupt
                    # data, so it outranks an unread GET.
                    severity="high" if endpoint["method"] in ("POST", "PUT", "PATCH", "DELETE") else "low",
                    reason="no test exercises this endpoint directly",
                    suggested_instruction=f"Add API checks for {key}",
                )
            )

        # ---- shared components ------------------------------------------- #
        shared = [
            name for name, entry in (application_map.components or {}).items() if entry.get("shared")
        ]
        report.components_total = len(shared)
        for name in sorted(shared):
            if str(name).strip().lower() in tested_components:
                report.components_covered += 1
                continue
            gaps.append(
                CoverageGap(
                    kind="component",
                    key=name,
                    label=name,
                    # Shared means a regression here breaks several pages.
                    severity="medium",
                    reason="a shared component that no test exercises",
                    suggested_instruction=f"Add coverage for the shared {name} component",
                )
            )

    # ---- requirements ---------------------------------------------------- #
    if graph is not None:
        coverage = graph.coverage()
        report.requirements_total = int(coverage.get("requirements", 0))
        report.requirements_covered = int(coverage.get("requirements_covered", 0))
        for node in graph.nodes.values():
            if node.kind != "requirement":
                continue
            if any(n.kind == "test" for n in graph.neighbours(node.id, depth=3)):
                continue
            gaps.append(
                CoverageGap(
                    kind="requirement",
                    key=node.key,
                    label=node.label or node.key,
                    severity="high",
                    reason="a known requirement with no test connected to it",
                    suggested_instruction=f"Automate: {node.label or node.key}",
                )
            )

    # ---- routes tested only on the happy path ---------------------------- #
    negative_by_route: dict[str, bool] = {}
    for test in tests:
        negative = "@negative" in test.tags
        for route in test.routes:
            key = _normalise_route(route)
            negative_by_route[key] = negative_by_route.get(key, False) or negative
    for route, has_negative in sorted(negative_by_route.items()):
        if not has_negative:
            gaps.append(
                CoverageGap(
                    kind="negative",
                    key=route,
                    label=route,
                    severity="medium",
                    reason="tested only on the happy path; no negative scenario",
                    suggested_instruction=f"Add validation and error-path scenarios for {route}",
                )
            )

    gaps.sort(key=lambda g: (SEVERITY_ORDER.index(g.severity), g.kind, g.key))
    report.gaps = gaps[:max_gaps]
    return report


# --------------------------------------------------------------------------- #
def _normalise_route(route: str) -> str:
    text = str(route).strip().lower().rstrip("/")
    return text or "/"


def _normalise_api(label: str) -> str:
    """`POST /residents` and `post /residents/` are the same endpoint."""
    text = str(label).strip().lower()
    parts = text.split(None, 1)
    if len(parts) == 2:
        return f"{parts[0]} {parts[1].rstrip('/') or '/'}"
    return text.rstrip("/") or "/"


def _route_severity(application_map: ApplicationMap, route: str) -> str:
    """A route with a form can change data; an untested one matters more."""
    raw = application_map.pages.get(route) or {}
    if raw.get("forms"):
        return "high"
    if raw.get("elements"):
        return "medium"
    return "low"


__all__ = ["CoverageGap", "CoverageReport", "analyse_coverage", "SEVERITY_ORDER"]
