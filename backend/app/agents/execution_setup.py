"""Builds and injects the optional login/search "setup steps" for an execution run.

The Execution Center UI lets users supply login credentials and a transaction
number/name directly as form fields (mirroring Module 1's Crawl page) instead of
requiring every uploaded feature file to hand-write its own
`Given I am logged in as maker ...` / `When I search for transaction ...` lines.
This module turns those optional fields into the equivalent step-DSL lines and
inserts them at the start of each feature file's scenario — every feature file gets
its own fresh Edge session (see `execution_orchestrator.run_execution`), so each one
needs its own login/search injected, not just the first.

Injection is skip-if-already-present: a feature file that already writes its own
login/search steps (e.g. one following the user's existing Java/Cucumber phrasing
convention) does NOT also get the backend-default login/search prepended — doing so
unconditionally caused a real bug, where a *second* login attempt fired while already
on a data-entry screen (no login form present), silently mis-typing into the wrong
field. Backend defaults exist to fill a *gap*, not to duplicate what's already there.
"""

import re

from app.agents.feature_step_parser import LoginStep, SearchStep, expand_scenario_outlines, parse_steps

_SCENARIO_LINE = re.compile(r"^\s*Scenario(?: Outline)?:", re.IGNORECASE)
_FEATURE_LINE = re.compile(r"^\s*Feature:", re.IGNORECASE)


def build_setup_steps_text(
    username: str | None,
    password: str | None,
    transaction_number: str | None,
    transaction_name: str | None,
    role: str = "maker",
    raw_text: str = "",
) -> str:
    """Returns the Gherkin lines to inject, or "" if nothing was supplied (or the
    feature file already has its own login/search step of that kind). Login and
    transaction search are independent — either, both, or neither may be injected."""
    # Expand outlines before checking for existing login/search steps so that
    # steps written inside a Scenario Outline are detected correctly.
    expanded = expand_scenario_outlines(raw_text) if raw_text else ""
    existing_steps = parse_steps(expanded) if expanded else []
    has_login = any(isinstance(s, LoginStep) for s in existing_steps)
    has_search = any(isinstance(s, SearchStep) for s in existing_steps)

    lines = []
    if username and password and not has_login:
        lines.append(f'Given I am logged in as {role} "{username}" with password "{password}"')

    query = transaction_number or transaction_name
    if query and not has_search:
        lines.append(f'When I search for transaction "{query}"')

    return "\n".join(lines)


def inject_setup_steps(raw_text: str, setup_text: str) -> str:
    """Inserts `setup_text`'s lines right after the first `Scenario:`/`Feature:` line
    (so the result still reads as a normal feature file), falling back to the very
    top of the file if neither marker is present."""
    if not setup_text:
        return raw_text

    lines = raw_text.splitlines()
    insert_at = None
    for index, line in enumerate(lines):
        if _SCENARIO_LINE.match(line):
            insert_at = index + 1
            break
    if insert_at is None:
        for index, line in enumerate(lines):
            if _FEATURE_LINE.match(line):
                insert_at = index + 1
                break
    if insert_at is None:
        insert_at = 0

    indent = "    "
    setup_lines = [f"{indent}{line}" for line in setup_text.splitlines()]
    new_lines = lines[:insert_at] + setup_lines + lines[insert_at:]
    return "\n".join(new_lines)
