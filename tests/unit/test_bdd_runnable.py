"""A feature file nothing runs is a document, not a test.

`bdd` was true whenever a BDD library appeared in `package.json`, which proves
only that somebody installed it. The demo repository had `@cucumber/cucumber`
*and* `playwright-bdd` in devDependencies, a `playwright test` script matching
`*.spec.ts`, and no wiring at all between the features directory and any
runner. Every `.feature` and `.steps.ts` the platform had ever written sat
there compiling cleanly and executing never — through the compile gate, through
standards, through a green run report.

It took running the suite by hand to notice, which is the whole reason these
exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.knowledge_service.indexer import detect_framework


def _repo(tmp_path: Path, **files: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for name, content in files.items():
        path = root / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


PACKAGE = json.dumps(
    {
        "devDependencies": {
            "@playwright/test": "^1.47.0",
            "@cucumber/cucumber": "^10.8.0",
            "playwright-bdd": "^7.1.0",
        }
    }
)


def test_installed_is_not_the_same_as_runnable(tmp_path: Path) -> None:
    """The exact shape of the demo repository, which ran nothing for months."""
    root = _repo(
        tmp_path,
        **{
            "package.json": PACKAGE,
            "playwright.config.ts": "export default defineConfig({ testDir: './tests' });",
            "tests__features__login.feature": "Feature: login\n  Scenario: x\n    Given y\n",
        },
    )
    info = detect_framework(root)
    assert info["bdd"] is True, "the library is installed, so this stays true"
    assert info["bdd_runnable"] is False, "but nothing would execute the feature"
    assert info["bdd_runner"] == ""


@pytest.mark.parametrize(
    ("config_name", "config_body", "expected"),
    [
        ("cucumber.cjs", "module.exports = { default: { paths: ['tests/features/**/*.feature'] } };", "cucumber-js"),
        ("cucumber.json", '{"default": {"paths": ["tests/features"]}}', "cucumber-js"),
    ],
)
def test_a_cucumber_config_makes_features_runnable(
    tmp_path: Path, config_name: str, config_body: str, expected: str
) -> None:
    root = _repo(tmp_path, **{"package.json": PACKAGE, config_name: config_body})
    info = detect_framework(root)
    assert info["bdd_runnable"] is True
    assert info["bdd_runner"] == expected


def test_playwright_bdd_wiring_lives_inside_the_playwright_config(tmp_path: Path) -> None:
    """There is no file to look for — the wiring is a function call."""
    root = _repo(
        tmp_path,
        **{
            "package.json": PACKAGE,
            "playwright.config.ts": (
                "import { defineBddConfig } from 'playwright-bdd';\n"
                "const testDir = defineBddConfig({ features: 'tests/features/*.feature' });\n"
            ),
        },
    )
    info = detect_framework(root)
    assert info["bdd_runnable"] is True
    assert info["bdd_runner"] == "playwright-bdd"


def test_a_repository_with_no_bdd_at_all_is_left_alone(tmp_path: Path) -> None:
    root = _repo(tmp_path, **{"package.json": json.dumps({"devDependencies": {"@playwright/test": "^1.47.0"}})})
    info = detect_framework(root)
    assert info["bdd"] is False
    assert info["bdd_runnable"] is False
