"""Visual regression specs.

Screenshots are already captured, so visual coverage is nearly free. It is also
the easiest kind of test to make worthless: point one at a page with a live
clock and it fails every run until somebody deletes it — and they delete the
whole suite, not just that test. These tests pin the restraint.
"""

from __future__ import annotations

from agents.code_generation.visual_renderer import (
    DEFAULT_MASK_PATTERNS,
    VisualCheck,
    render_visual_tests,
    spec_file_name,
    visual_checks_from,
)
from services.knowledge_service.application_map import ApplicationMap, PageKnowledge


# --------------------------------------------------------------------------- #
# Which routes qualify
# --------------------------------------------------------------------------- #
def test_a_page_is_not_baselined_until_its_markup_settles() -> None:
    amap = ApplicationMap()
    page = PageKnowledge(route="/residents/new", dom_hash="aaa", title="New resident")

    amap.put_page(page)
    assert amap.visual_candidates() == [], "one sighting is not stability"

    amap.put_page(PageKnowledge(route="/residents/new", dom_hash="aaa"))
    amap.put_page(PageKnowledge(route="/residents/new", dom_hash="aaa"))
    assert [c["route"] for c in amap.visual_candidates()] == ["/residents/new"]


def test_changing_markup_resets_the_stability_counter() -> None:
    amap = ApplicationMap()
    for dom_hash in ("aaa", "aaa", "aaa"):
        amap.put_page(PageKnowledge(route="/dashboard", dom_hash=dom_hash))
    assert amap.visual_candidates()

    amap.put_page(PageKnowledge(route="/dashboard", dom_hash="bbb"))
    assert amap.visual_candidates() == [], "the page changed; it is not settled any more"


def test_a_page_never_rendered_in_a_browser_is_not_baselined() -> None:
    """An HTTP capture is not what a user sees, so its screenshot proves nothing."""
    amap = ApplicationMap()
    for _ in range(4):
        amap.put_page(PageKnowledge(route="/x", dom_hash="aaa", simulated=True))
    assert amap.visual_candidates() == []


def test_a_screenshot_is_kept_when_a_later_capture_has_none() -> None:
    amap = ApplicationMap()
    amap.put_page(PageKnowledge(route="/x", dom_hash="aaa", screenshot_path="shots/x.png"))
    amap.put_page(PageKnowledge(route="/x", dom_hash="aaa"))
    assert amap.page("/x").screenshot_path == "shots/x.png"


# --------------------------------------------------------------------------- #
# What is generated
# --------------------------------------------------------------------------- #
def test_pixel_comparison_is_delegated_to_playwright() -> None:
    """Reimplementing baseline management in Python would be strictly worse."""
    source = render_visual_tests(visual_checks_from([{"route": "/x"}]), suite="X")
    assert "toHaveScreenshot(" in source
    assert "--update-snapshots" in source, "the accept-a-change workflow must be stated"


def test_dynamic_regions_are_masked_rather_than_tolerated() -> None:
    source = render_visual_tests(visual_checks_from([{"route": "/x"}]), suite="X")
    for pattern in DEFAULT_MASK_PATTERNS:
        assert pattern in source
    # Masking beats raising the threshold: a loose threshold hides real regressions.
    assert "maxDiffPixelRatio: 0.01" in source


def test_the_page_is_settled_before_the_screenshot_is_taken() -> None:
    """Otherwise the first run bakes a half-loaded page into the baseline."""
    source = render_visual_tests(visual_checks_from([{"route": "/x"}]), suite="X")
    assert source.index("waitForLoadState('networkidle')") < source.index("toHaveScreenshot(")


def test_each_route_is_checked_at_desktop_and_mobile() -> None:
    source = render_visual_tests(visual_checks_from([{"route": "/x"}]), suite="X")
    assert source.count("test(") == 2
    assert "width: 1280" in source and "width: 375" in source


def test_snapshot_names_are_unique_per_route_and_viewport() -> None:
    checks = visual_checks_from([{"route": "/a"}, {"route": "/b/c"}])
    source = render_visual_tests(checks, suite="X")
    names = [line for line in source.splitlines() if "toHaveScreenshot(" in line]
    assert len(names) == len(set(names)) == 4


def test_generated_source_is_balanced() -> None:
    source = render_visual_tests(visual_checks_from([{"route": "/a"}, {"route": "/b"}]), suite="X")
    assert source.count("{") == source.count("}")
    assert source.count("[") == source.count("]")
    assert source.rstrip().endswith("});")


def test_a_quote_in_a_title_cannot_break_the_spec() -> None:
    checks = [VisualCheck(route="/x", name="Tom's page")]
    source = render_visual_tests(checks, suite="X")
    assert "Tom\\'s page" in source


def test_no_candidates_means_no_tests_rather_than_an_empty_describe() -> None:
    assert visual_checks_from([]) == []


def test_spec_file_names_are_kebab_case() -> None:
    assert spec_file_name("Resident Registration") == "resident-registration.visual.spec.ts"
