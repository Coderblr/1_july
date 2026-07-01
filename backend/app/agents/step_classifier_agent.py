"""LLM-based Gherkin Step Classifier.

Called as the last resort by feature_step_parser._parse_step_text() when both the
regex pattern list and the keyword-inference fallback fail to classify a step.

Architecture
------------
1. All classifications are cached to a JSON file on disk (storage/step_cache.json).
   This means every unique step text is sent to the LLM at most once across all
   execution runs — even across server restarts. With 2000 feature files that reuse
   the same step phrasings, the LLM is called only for genuinely novel steps.

2. The function is null-safe: if Azure OpenAI is not configured, or the API call
   fails, it returns None and the caller falls through to UnrecognizedStep → graceful
   skip.  The platform never hard-fails because the LLM is unavailable.

3. The returned dict keys mirror what _build_step_from_classification() expects:
     {"type": "fill|click|select|login|search|approve|logout|skip",
      "field_name": "...", "value": "...", "button_text": "...",
      "username": "...", "password": "...", "query": "...", "reason": "..."}
"""

import json
import logging
import re
from pathlib import Path

from app.core.config import STORAGE_DIR
from app.llm.azure_openai_client import invoke_llm

logger = logging.getLogger(__name__)

_CACHE_FILE = STORAGE_DIR / "step_cache.json"
_cache: dict[str, dict] = {}
_cache_dirty = False
_cache_loaded = False

_SYSTEM_PROMPT = """\
You classify Gherkin test steps written for an NBC banking automation suite.

Given a step text, decide what Selenium action (if any) it represents and return
a single JSON object.  Never return anything other than the JSON.

Allowed types and their required extra keys:
  fill    → field_name (str), value (str)   — fill an input field
  click   → button_text (str)               — click a button or link
  select  → field_name (str), value (str)   — choose from a dropdown or radio group
  login   → username (str), password (str)  — log into the application
  search  → query (str)                     — search/navigate to a screen by code
  approve → (no extra keys)                 — authorise/approve a transaction
  logout  → (no extra keys)                 — log out of the application
  skip    → reason (str)                    — assertion, data collection, or ambiguous

Rules:
- Use "skip" for assertions (verify, check, confirm, collect, gather, should, etc.)
- Use "skip" if there is no concrete value to fill or button to click
- Extract the quoted token(s) as value for fill/select
- Extract the button / link label for click (e.g. "Submit", "Refresh", "OK")
- For fill: field_name is the label / placeholder the input maps to (e.g. "Account Number")
- Return only raw JSON — no markdown fences, no explanation.
"""


def _load_cache() -> None:
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    try:
        if _CACHE_FILE.exists():
            _cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            logger.debug("Step classifier: loaded %d cached classifications", len(_cache))
    except Exception:
        _cache = {}
    _cache_loaded = True


def _flush_cache() -> None:
    if not _cache_dirty:
        return
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(_cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("Step classifier: could not write cache — %s", exc)


def classify_step(text: str) -> dict | None:
    """Classify a single Gherkin step text using Azure OpenAI.

    Returns a dict with at least a "type" key, or None if classification failed or
    the LLM is not configured.  Results are cached persistently across restarts.
    """
    global _cache_dirty
    _load_cache()

    key = text.strip()
    if key in _cache:
        return _cache[key]

    raw_response = invoke_llm(_SYSTEM_PROMPT, f'Step: "{key}"')
    if not raw_response:
        return None

    try:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_response.strip(), flags=re.MULTILINE)
        result: dict = json.loads(clean)
        if "type" not in result:
            return None
        _cache[key] = result
        _cache_dirty = True
        _flush_cache()
        logger.debug("Step classifier: '%s' → %s", key[:60], result.get("type"))
        return result
    except Exception as exc:
        logger.debug("Step classifier: failed to parse LLM response — %s", exc)
        return None
