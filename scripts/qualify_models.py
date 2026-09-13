"""Find out which free models can actually do this platform's work.

The set of free models on OpenRouter churns constantly — models appear, become
paid, get retired, and a `models.yaml` written six months ago routes to things
that no longer exist. Guessing from a name is worthless: "coder" in an id says
nothing about whether the model can return parseable JSON under load.

So this asks them. Each candidate gets the two requests that matter here:

1. **Structured output.** Every agent calls `ask_json`. A model that writes a
   preamble before its JSON, or fences it, or truncates it, cannot be used for
   anything — that is the single hard requirement.
2. **Code generation.** The coding tier has to emit a compact plan the renderer
   can consume.

Models are then ranked by whether they passed, then by latency. Run it whenever
runs start failing for no obvious reason:

    python -m scripts.qualify_models
    python -m scripts.qualify_models --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import httpx  # noqa: E402 - imported after sys.path is extended above

BASE_URL = "https://openrouter.ai/api/v1"

#: Ids that are free but not general-purpose chat models. Sending them a QA
#: prompt wastes a request and pollutes the results.
_EXCLUDED_SUBSTRINGS = (
    "lyria",            # music generation
    "content-safety",   # a classifier, not an assistant
    "-sante",           # health-domain specialist
    "-fin:",            # finance-domain specialist
    "whisper",
    "embed",
    "tts",
    "image",
)

JSON_PROBE_SYSTEM = (
    "You output ONLY one JSON object. No prose, no markdown fences, no explanation."
)
JSON_PROBE_USER = """A QA engineer asked: "Automate the resident registration form".

Reply with ONE JSON object and nothing else:
{"title": str, "criteria": [str], "priority": "P1|P2|P3", "negative_cases": int}"""

CODE_PROBE_SYSTEM = (
    "You plan test automation. You output ONLY one JSON object: no prose, no fences."
)
CODE_PROBE_USER = """Verified locator catalogue (the ONLY locators you may use):
[{"name": "Full name", "role": "textbox", "locator": "getByTestId('resident-name')"},
 {"name": "Email", "role": "textbox", "locator": "getByTestId('resident-email')"},
 {"name": "Create resident", "role": "button", "locator": "getByTestId('resident-submit')"}]

Plan ONE Page Object for route /residents/new. Reply with ONE JSON object:
{"pages": [{"class": str, "route": str,
  "locators": [{"prop": str, "locator": str}],
  "methods": [{"name": str, "kind": "action|assertion", "params": [str], "locators": [str]}]}]}"""


@dataclass
class ModelResult:
    model: str
    context: int = 0
    json_ok: bool = False
    code_ok: bool = False
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.json_ok

    @property
    def score(self) -> tuple[int, float]:
        # Passing both probes beats passing one; among equals, faster wins.
        return (-(int(self.json_ok) + int(self.code_ok)), self.latency_s)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["usable"] = self.usable
        return payload


def _extract_json(text: str) -> dict | None:
    """Parse a model's answer, tolerating the usual wrappers.

    Tolerated, but recorded: a model that needs unwrapping is more fragile than
    one that does not, and that shows up in the notes.
    """
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


async def _ask(
    client: httpx.AsyncClient, key: str, model: str, system: str, user: str, timeout: float
) -> tuple[str, dict, str]:
    """One completion. Returns (text, usage, error)."""
    try:
        response = await client.post(
            f"{BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://github.com/aiqa-engineer",
                "X-Title": "AI QA Engineer",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.1,
                "max_tokens": 800,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return "", {}, f"transport: {type(exc).__name__}"

    if response.status_code == 429:
        return "", {}, "rate limited"
    if response.status_code >= 400:
        detail = ""
        try:
            detail = str((response.json().get("error") or {}).get("message", ""))[:90]
        except Exception:  # noqa: BLE001
            detail = response.text[:90]
        return "", {}, f"HTTP {response.status_code}: {detail}"

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return "", {}, "no choices returned"
    text = (choices[0].get("message") or {}).get("content") or ""
    return text, data.get("usage") or {}, ""


async def qualify(model: str, context: int, key: str, timeout: float = 90.0) -> ModelResult:
    result = ModelResult(model=model, context=context)
    started = time.monotonic()

    async with httpx.AsyncClient() as client:
        text, usage, error = await _ask(client, key, model, JSON_PROBE_SYSTEM, JSON_PROBE_USER, timeout)
        result.latency_s = round(time.monotonic() - started, 2)
        if error:
            result.error = error
            return result

        result.prompt_tokens = int(usage.get("prompt_tokens", 0))
        result.completion_tokens = int(usage.get("completion_tokens", 0))

        if not text.strip():
            result.error = "empty response"
            return result
        if text.strip().startswith("```"):
            result.notes.append("wraps JSON in fences")

        parsed = _extract_json(text)
        if parsed is None:
            result.error = f"unparseable: {text.strip()[:70]}"
            return result
        result.json_ok = True
        if not {"title", "criteria"} <= set(parsed):
            result.notes.append("ignored the requested schema")

        code_text, _usage, code_error = await _ask(
            client, key, model, CODE_PROBE_SYSTEM, CODE_PROBE_USER, timeout
        )
        if code_error:
            result.notes.append(f"code probe: {code_error}")
            return result
        code = _extract_json(code_text)
        if code and isinstance(code.get("pages"), list) and code["pages"]:
            page = code["pages"][0]
            result.code_ok = bool(page.get("class") and page.get("methods"))
            if not result.code_ok:
                result.notes.append("returned a page with no methods")
        else:
            result.notes.append("no usable page plan")

    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify OpenRouter free models.")
    parser.add_argument("--json", default="", help="Write full results to this path.")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--limit", type=int, default=0, help="Only try the N largest-context models.")
    args = parser.parse_args()

    from configs.settings import get_settings

    settings = get_settings()
    key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("No OPENROUTER_API_KEY configured.")
        return 1

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f"{BASE_URL}/models", headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        catalogue = response.json().get("data", [])

    candidates: list[tuple[str, int]] = []
    for entry in catalogue:
        model_id = str(entry.get("id", ""))
        pricing = entry.get("pricing") or {}
        try:
            if float(pricing.get("prompt", 1) or 0) or float(pricing.get("completion", 1) or 0):
                continue
        except (TypeError, ValueError):
            continue
        if any(token in model_id.lower() for token in _EXCLUDED_SUBSTRINGS):
            continue
        candidates.append((model_id, int(entry.get("context_length", 0) or 0)))

    candidates.sort(key=lambda pair: -pair[1])
    if args.limit:
        candidates = candidates[: args.limit]

    print(f"Qualifying {len(candidates)} free chat model(s)\n")
    print(f"  {'model':<52} {'ctx':>9}  json  code  {'sec':>6}  notes")
    print(f"  {'-' * 100}")

    results: list[ModelResult] = []
    for model_id, context in candidates:
        result = await qualify(model_id, context, key, timeout=args.timeout)
        results.append(result)
        flag = lambda ok: " ok " if ok else " -- "  # noqa: E731
        note = result.error or "; ".join(result.notes)
        print(
            f"  {model_id:<52} {context:>9,}  {flag(result.json_ok)}  {flag(result.code_ok)}  "
            f"{result.latency_s:>6.1f}  {note[:38]}"
        )

    results.sort(key=lambda r: r.score)
    usable = [r for r in results if r.usable]

    print(f"\n{'=' * 104}")
    print(f"{len(usable)} of {len(results)} model(s) can return usable JSON")
    print(f"{'=' * 104}")
    for result in usable[:12]:
        badge = "json+code" if result.code_ok else "json only"
        print(f"  {badge:<10} {result.latency_s:>6.1f}s  {result.model}")

    if not usable:
        print("  None. Every candidate failed; the run would fall back to offline mode.")

    if args.json:
        Path(args.json).write_text(
            json.dumps([r.to_dict() for r in results], indent=2), encoding="utf-8"
        )
        print(f"\nfull results -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
