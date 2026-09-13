"""Autonomous exploratory testing.

Every other part of this platform starts from a requirement: somebody says what
the software should do, and tests are written to check it. This part starts from
nothing but the running application and asks a narrower question — *is anything
here self-evidently broken?*

**What a spec-free oracle can and cannot decide.** Without a requirement there is
no way to know whether a form should accept a 30-character surname, so this
module does not guess. It looks only for failures that need no specification to
recognise:

* the server returned 5xx
* a page rendered a stack trace or framework error page
* a link in the navigation leads nowhere
* a form with required fields accepted an empty submission
* a security header that every page should carry is missing

That list is deliberately short. A finding here is evidence of a defect, not a
hunch, because an exploratory report full of maybes is a report nobody reads.
Anything ambiguous is reported as an *observation* — surfaced, not asserted.

**Findings are not tests.** A probe that fires produces a finding with evidence
attached. Turning that into a permanent regression test is a separate, explicit
step, because a test written against a bug that is about to be fixed is a test
that will fail tomorrow for the right reason and be deleted for the wrong one.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

#: Text that only appears when something has gone wrong server-side. Matched
#: case-insensitively against a page body.
_ERROR_SIGNATURES = (
    "traceback (most recent call last)",
    "internal server error",
    "exception in thread",
    "stack trace:",
    "sqlalchemy.exc.",
    "django.core.exceptions",
    "org.springframework",
    "system.nullreferenceexception",
    "fatal error:",
    "warning: mysqli",
    "undefined index:",
)

#: Headers a browser-facing page should carry. Their absence is a real finding
#: but a low-severity one: it is a hardening gap, not a broken feature.
_EXPECTED_SECURITY_HEADERS = ("x-content-type-options", "x-frame-options")

SEVERITY_ORDER = ("critical", "high", "medium", "low", "observation")


@dataclass
class ExploratoryFinding:
    """Something wrong, with the evidence that says so."""

    kind: str                       # server_error | error_page | broken_link | missing_validation | hardening
    route: str
    title: str
    severity: str = "medium"
    evidence: str = ""
    reproduction: list[str] = field(default_factory=list)
    confidence: str = "confirmed"   # confirmed | observation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Probe:
    """One thing to try, and where."""

    kind: str
    route: str
    method: str = "GET"
    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExploratoryReport:
    probes_planned: int = 0
    probes_run: int = 0
    findings: list[ExploratoryFinding] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> list[ExploratoryFinding]:
        return [f for f in self.findings if f.confidence == "confirmed"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "probes_planned": self.probes_planned,
            "probes_run": self.probes_run,
            "finding_count": len(self.findings),
            "confirmed_count": len(self.confirmed),
            "findings": [f.to_dict() for f in self.findings],
            "unreachable": self.unreachable,
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if not self.findings:
            return (
                f"{self.probes_run} probe(s) run, nothing self-evidently broken. "
                "This is not a claim that the application is correct — only that "
                "no probe found a crash, error page, dead link or missing validation."
            )
        confirmed = len(self.confirmed)
        observations = len(self.findings) - confirmed
        text = f"{confirmed} confirmed finding(s) from {self.probes_run} probe(s)"
        if observations:
            text += f", plus {observations} observation(s) needing a human"
        return text + "."


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def plan_probes(application_map: Any, *, max_probes: int = 40) -> list[Probe]:
    """Decide what to try, from what exploration already found.

    Only routes and forms the application itself revealed are probed. Guessing
    at URLs would turn this into a vulnerability scanner pointed at somebody
    else's infrastructure, which is not what a QA tool should do unasked.
    """
    if application_map is None:
        return []

    from services.knowledge_service.application_map import PageKnowledge, route_of

    known = set(application_map.pages)
    probes: list[Probe] = []
    for route, raw in sorted(application_map.pages.items()):
        page = PageKnowledge(**raw)
        probes.append(
            Probe(kind="reachable", route=route, rationale="a known route should render")
        )

        for target in page.navigations:
            # `/residents/1/edit` is an instance of the known pattern
            # `/residents/:id/edit`, not an unexplored page. Comparing the raw
            # string would report a dead link for every id-bearing URL in the
            # application.
            if target and target not in known and route_of(target) not in known:
                probes.append(
                    Probe(
                        kind="broken_link",
                        route=target,
                        rationale=f"linked from {route} but never successfully explored",
                    )
                )

        # An empty submission to a form with required fields should be rejected.
        # If it succeeds, validation is missing — and that needs no spec to know.
        required = [
            locator.name
            for locator in page.locators()
            if locator.required and locator.role in ("textbox", "combobox")
        ]
        for form in page.forms:
            action = str(form.get("action") or "").strip()
            method = str(form.get("method") or "get").upper()
            if required and action and method in ("POST", "PUT", "PATCH"):
                probes.append(
                    Probe(
                        kind="missing_validation",
                        route=action,
                        method=method,
                        payload={},
                        rationale=(
                            f"{route} declares {len(required)} required field(s) "
                            f"({', '.join(required[:3])}); an empty submission must be rejected"
                        ),
                    )
                )

    # De-duplicate, keeping the first rationale for each (kind, route, method).
    seen: set[tuple[str, str, str]] = set()
    unique: list[Probe] = []
    for probe in probes:
        key = (probe.kind, probe.route, probe.method)
        if key in seen:
            continue
        seen.add(key)
        unique.append(probe)
    return unique[:max_probes]


# --------------------------------------------------------------------------- #
# Judging one response
# --------------------------------------------------------------------------- #
def evaluate(probe: Probe, status: int, body: str, headers: dict[str, str] | None = None) -> list[ExploratoryFinding]:
    """Turn one probe result into findings. Deterministic and spec-free."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    body_text = body or ""
    lowered = body_text.lower()
    findings: list[ExploratoryFinding] = []

    if status >= 500:
        findings.append(
            ExploratoryFinding(
                kind="server_error",
                route=probe.route,
                title=f"{probe.method} {probe.route} returned HTTP {status}",
                severity="critical",
                evidence=_snippet(body_text),
                reproduction=[f"{probe.method} {probe.route}", f"observe HTTP {status}"],
            )
        )
        return findings                      # the 5xx is the story; stop here

    for signature in _ERROR_SIGNATURES:
        if signature in lowered:
            findings.append(
                ExploratoryFinding(
                    kind="error_page",
                    route=probe.route,
                    title=f"{probe.route} rendered a server error page",
                    severity="high",
                    evidence=_snippet(body_text, around=signature),
                    reproduction=[f"{probe.method} {probe.route}", "observe the stack trace in the response"],
                )
            )
            break

    if probe.kind == "broken_link" and status == 404:
        findings.append(
            ExploratoryFinding(
                kind="broken_link",
                route=probe.route,
                title=f"{probe.route} is linked but returns 404",
                severity="medium",
                evidence=f"HTTP {status}. {probe.rationale}",
                reproduction=[f"follow the link to {probe.route}", "observe HTTP 404"],
            )
        )

    if probe.kind == "missing_validation" and 200 <= status < 300 and not _looks_rejected(lowered):
        findings.append(
            ExploratoryFinding(
                kind="missing_validation",
                route=probe.route,
                title=f"{probe.route} accepted a submission with every required field empty",
                severity="high",
                evidence=f"HTTP {status} with no validation message in the response. {probe.rationale}",
                reproduction=[
                    f"{probe.method} {probe.route} with an empty body",
                    f"observe HTTP {status} instead of a validation error",
                ],
            )
        )

    if probe.kind == "reachable" and 200 <= status < 300:
        missing = [h for h in _EXPECTED_SECURITY_HEADERS if h not in headers]
        if missing:
            findings.append(
                ExploratoryFinding(
                    kind="hardening",
                    route=probe.route,
                    title=f"{probe.route} is missing {', '.join(missing)}",
                    severity="low",
                    # Not every application needs these, and some set them at the
                    # edge rather than the app, so this is surfaced not asserted.
                    confidence="observation",
                    evidence=f"response headers: {', '.join(sorted(headers)) or 'none'}",
                    reproduction=[f"GET {probe.route}", "inspect the response headers"],
                )
            )

    return findings


def _looks_rejected(lowered_body: str) -> bool:
    """Did the application say no, even with a 2xx status?

    Plenty of applications re-render the form with errors and a 200. Treating
    that as missing validation would be a false accusation.
    """
    return bool(
        re.search(
            r"\b(is required|required field|please (enter|provide|fill)|cannot be (empty|blank)"
            r"|must not be (empty|blank)|invalid|validation (error|failed))\b",
            lowered_body,
        )
    )


def _snippet(body: str, around: str = "", width: int = 220) -> str:
    if not body:
        return "(empty response body)"
    if around:
        index = body.lower().find(around)
        if index >= 0:
            start = max(0, index - width // 3)
            return body[start : start + width].strip()
    return body[:width].strip()


# --------------------------------------------------------------------------- #
def collapse(findings: list[ExploratoryFinding]) -> list[ExploratoryFinding]:
    """Fold a site-wide observation into one row.

    Six identical "missing security header" lines, one per route, bury the one
    finding that actually needs attention. A repeated observation is a single
    property of the application, so it is reported once with the affected
    routes listed.
    """
    hardening = [f for f in findings if f.kind == "hardening"]
    if len(hardening) < 2:
        return findings

    routes = sorted({f.route for f in hardening})
    missing = hardening[0].title.split(" is missing ", 1)[-1]
    merged = ExploratoryFinding(
        kind="hardening",
        route="(site-wide)",
        title=f"{len(routes)} route(s) missing {missing}",
        severity="low",
        confidence="observation",
        evidence=f"affects {', '.join(routes[:8])}" + (" and others" if len(routes) > 8 else ""),
        reproduction=[f"GET {routes[0]}", "inspect the response headers"],
    )
    return [f for f in findings if f.kind != "hardening"] + [merged]


def rank(findings: list[ExploratoryFinding]) -> list[ExploratoryFinding]:
    return sorted(
        collapse(findings),
        key=lambda f: (SEVERITY_ORDER.index(f.severity) if f.severity in SEVERITY_ORDER else 9, f.route),
    )


def regression_instruction(finding: ExploratoryFinding) -> str:
    """The run that would turn a confirmed finding into a permanent test.

    Deliberately phrased as "add a test that asserts the correct behaviour",
    not "reproduce the bug": once the defect is fixed, the test must still be
    meaningful, otherwise it gets deleted along with the fix.
    """
    if finding.kind == "missing_validation":
        return (
            f"Add a negative test asserting that {finding.route} rejects a submission "
            "with empty required fields and shows a validation message"
        )
    if finding.kind == "broken_link":
        return f"Add a navigation test asserting that {finding.route} loads successfully"
    if finding.kind in ("server_error", "error_page"):
        return f"Add a test asserting that {finding.route} renders successfully without a server error"
    return f"Add coverage for {finding.route}"


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def run_probes(
    probes: list[Probe],
    base_url: str,
    *,
    timeout: float = 15.0,
    max_probes: int = 40,
) -> ExploratoryReport:
    """Execute the probes over plain HTTP.

    HTTP rather than a browser on purpose: every probe here is about what the
    *server* did — a status code, an error page, a rejected submission — and
    none of it needs a rendering engine. That keeps this cheap enough to run on
    every deploy, which is the only way exploratory testing gets done at all.
    """
    import httpx

    report = ExploratoryReport(probes_planned=len(probes))
    if not base_url:
        return report

    root = base_url.rstrip("/")
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for probe in probes[:max_probes]:
            # A route pattern is a template, not an address. Probing
            # `/residents/:id/edit` would test the literal string ":id".
            if ":" in probe.route or "{" in probe.route:
                continue
            target = probe.route if probe.route.startswith("http") else f"{root}/{probe.route.lstrip('/')}"
            try:
                if probe.method == "GET":
                    response = client.get(target)
                else:
                    response = client.request(probe.method, target, data=probe.payload or {})
            except httpx.HTTPError as exc:
                report.unreachable.append(f"{probe.method} {probe.route}: {exc}")
                continue

            report.probes_run += 1
            report.findings.extend(
                evaluate(probe, response.status_code, response.text, dict(response.headers))
            )

    report.findings = rank(report.findings)
    return report


def explore(application_map: Any, base_url: str, *, max_probes: int = 40) -> ExploratoryReport:
    """Plan and run in one call."""
    return run_probes(plan_probes(application_map, max_probes=max_probes), base_url, max_probes=max_probes)


__all__ = [
    "ExploratoryFinding",
    "ExploratoryReport",
    "Probe",
    "SEVERITY_ORDER",
    "collapse",
    "evaluate",
    "explore",
    "plan_probes",
    "run_probes",
    "rank",
    "regression_instruction",
]
