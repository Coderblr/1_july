"""Feature Step DSL Parser.

The Execution Engine maps each Gherkin step onto a typed action object that
step_executor.py can run against a live Selenium session. The parser uses an
ordered list of regexes so every step is classified — either as an executable
action (LoginStep, FillStep, ClickStep, …) or as a SkippedStep / UnrecognizedStep
that the executor handles gracefully (no hard failures on assertion-only steps).

Design goals for "any feature file":
  1. Executable steps  → run them (fill, click, search, login, approve, logout …)
  2. Assertion steps   → skip gracefully (check, verify, collect, confirm …)
  3. Unrecognized steps → skip gracefully with a log warning; never crash the run

Pattern ordering matters: put more-specific patterns before broader ones, and all
action patterns before the assertion skip-block and the last-resort catch-all.
"""

import re
from dataclasses import dataclass

_STEP_LINE = re.compile(r"^\s*(?:Given|When|Then|And|But)\s+(.*)$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Scenario Outline expansion
# ---------------------------------------------------------------------------

_SCENARIO_OUTLINE_RE = re.compile(r"^(\s*)Scenario Outline\s*:(.*)", re.IGNORECASE)
_EXAMPLES_RE = re.compile(r"^\s*Examples\s*:", re.IGNORECASE)
_NEXT_BLOCK_RE = re.compile(
    r"^\s*(?:Scenario\b|Scenario Outline\b|Feature\b|Background\b|Rule\b)",
    re.IGNORECASE,
)
_TABLE_ROW_RE = re.compile(r"^\s*\|")


def expand_scenario_outlines(raw_text: str) -> str:
    """Replace every Scenario Outline + Examples block with concrete Scenario instances.

    Feature files written with Scenario Outline have placeholder tokens like
    <account> and <amount> — those are not real values and cannot be executed.
    This function substitutes each token with the actual cell value from the
    Examples table, producing one concrete Scenario per row so that parse_steps()
    receives real values that can be resolved against the Locator Repository.
    """
    lines = raw_text.splitlines()
    output: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        m = _SCENARIO_OUTLINE_RE.match(line)
        if not m:
            output.append(line)
            i += 1
            continue

        indent, title = m.group(1), m.group(2).strip()
        i += 1

        # Collect step lines until Examples: or next top-level block
        step_lines: list[str] = []
        while i < len(lines):
            cur = lines[i]
            if _EXAMPLES_RE.match(cur):
                break
            if _NEXT_BLOCK_RE.match(cur):
                break
            step_lines.append(cur)
            i += 1

        # Parse one or more Examples blocks
        expanded_any = False
        while i < len(lines) and _EXAMPLES_RE.match(lines[i]):
            i += 1  # skip "Examples:" line

            # Skip tag lines and blank lines before the table header
            while i < len(lines) and not _TABLE_ROW_RE.match(lines[i]):
                i += 1
            if i >= len(lines):
                break

            # Header row
            headers = [h.strip() for h in lines[i].strip().strip("|").split("|")]
            i += 1

            # Data rows
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                i += 1
                if len(cells) != len(headers):
                    continue
                row = dict(zip(headers, cells))
                output.append(f"{indent}Scenario: {title}")
                for sl in step_lines:
                    expanded = sl
                    for k, v in row.items():
                        expanded = expanded.replace(f"<{k}>", v)
                    output.append(expanded)
                output.append("")
                expanded_any = True

        if not expanded_any:
            # No Examples table found — emit the outline unchanged.
            output.append(f"{indent}Scenario Outline: {title}")
            output.extend(step_lines)

    return "\n".join(output)


# ---------------------------------------------------------------------------
# Step dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LoginStep:
    role: str  # "maker" | "checker"
    username: str
    password: str
    raw: str


@dataclass
class SearchStep:
    query: str
    raw: str


@dataclass
class FillStep:
    field_name: str
    value: str
    raw: str


@dataclass
class SelectStep:
    field_name: str
    value: str
    raw: str


@dataclass
class CheckStep:
    field_name: str
    raw: str


@dataclass
class ClickStep:
    button_text: str
    raw: str


@dataclass
class ShortEnquiryStep:
    """Fill Account Number in an NBC short-enquiry screen and click Submit.

    Maps 'we search for the deposit X short enquiry' — fills the account field,
    tabs to trigger the account lookup, dismisses the Customer Details popup,
    then clicks Submit so account details are shown before the next step runs.
    """
    account_number: str
    raw: str


@dataclass
class SubmitForApprovalStep:
    raw: str


@dataclass
class ApproveStep:
    raw: str


@dataclass
class LogoutStep:
    raw: str


@dataclass
class AssertVisibleStep:
    field_name: str
    raw: str


@dataclass
class SkippedStep:
    """Step that is recognised but cannot be executed by this platform.

    Covers assertion/verification steps (check, verify, collect, confirm…),
    data-collection steps that depend on a Java HashMap built by the Java
    framework, and complex multi-step operations with no direct Selenium
    equivalent. The step is logged with status 'skipped' in the execution
    report so the user can see it was encountered but not run — the
    execution continues rather than aborting on an assertion line.
    """
    reason: str
    raw: str


@dataclass
class UnrecognizedStep:
    raw: str


Step = (
    LoginStep
    | SearchStep
    | FillStep
    | SelectStep
    | CheckStep
    | ClickStep
    | ShortEnquiryStep
    | SubmitForApprovalStep
    | ApproveStep
    | LogoutStep
    | AssertVisibleStep
    | SkippedStep
    | UnrecognizedStep
)

# ---------------------------------------------------------------------------
# Pattern list
# ---------------------------------------------------------------------------
# Rules:
#   • More-specific patterns come BEFORE general ones.
#   • All action patterns come BEFORE assertion/skip patterns.
#   • Assertion/skip patterns come BEFORE the last-resort catch-all fill.
#   • UnrecognizedStep is the implicit final fallback (no pattern needed).

_PATTERNS: list[tuple[re.Pattern, str]] = [

    # ── Login ────────────────────────────────────────────────────────────────
    # Canonical DSL
    (re.compile(r'^I am logged in as (maker|checker) "([^"]*)" with password "([^"]*)"$', re.IGNORECASE), "login"),
    # NBC Java/Cucumber phrasing — credential tokens resolved at runtime
    (re.compile(r'^the user is logged into NBC using "([^"]*)" and "([^"]*)"$', re.IGNORECASE), "login_alias"),
    (re.compile(r'^the user is logged into NBC using "([^"]*)" and "([^"]*)"$', re.IGNORECASE), "login_alias"),

    # ── Search ───────────────────────────────────────────────────────────────
    (re.compile(r'^I search for transaction "([^"]*)"$', re.IGNORECASE), "search"),
    (re.compile(r'^we search for the transaction "([^"]*)"$', re.IGNORECASE), "search"),

    # ── Short-enquiry compound step ──────────────────────────────────────────
    (re.compile(r'^we search for the deposit "([^"]*)" short enquiry$', re.IGNORECASE), "short_enquiry"),

    # ── Fill ─────────────────────────────────────────────────────────────────
    (re.compile(r'^I fill "([^"]*)" with "([^"]*)"$', re.IGNORECASE), "fill"),
    # "we enter the the account as X …"  /  "we enter the amount as X …"
    (re.compile(r'^we enter the(?: the)? (.+?) as "([^"]*)"(?: in .+ screen)?$', re.IGNORECASE), "fill"),
    # "we enter narration as X"  /  "we enter X as Y"
    (re.compile(r'^we enter (?:the )?(.+?) as "([^"]*)"(?: in .+ screen)?$', re.IGNORECASE), "fill"),
    # "enter the X as Y"
    (re.compile(r'^enter (?:the )?(.+?) as "([^"]*)"(?: in .+ screen)?$', re.IGNORECASE), "fill"),
    # "I enter X as Y"  /  "I enter the X as Y"
    (re.compile(r'^I enter (?:the )?(.+?) as "([^"]*)"(?: in .+ screen)?$', re.IGNORECASE), "fill"),
    # "fill X with Y"  /  "fill the X with Y"
    (re.compile(r'^fill (?:the )?(.+?) with "([^"]*)"(?: in .+ screen)?$', re.IGNORECASE), "fill"),
    # "input X as Y"  /  "we input X as Y"
    (re.compile(r'^(?:we )?input (?:the )?(.+?) (?:as|with) "([^"]*)"(?: in .+ screen)?$', re.IGNORECASE), "fill"),
    # "type "Y" in/into the X" — value before field
    (re.compile(r'^(?:we )?type "([^"]+)" (?:in|into) (?:the )?(.+?)$', re.IGNORECASE), "fill_value_first"),

    # ── Select ───────────────────────────────────────────────────────────────
    (re.compile(r'^I select "([^"]*)" as "([^"]*)"$', re.IGNORECASE), "select"),
    (re.compile(r'^we select the (.+?) as "([^"]*)"(?: in .+ screen)?$', re.IGNORECASE), "select"),
    (re.compile(r'^we select (.+?) as "([^"]*)"(?: in .+ screen)?$', re.IGNORECASE), "select"),
    # "we select State with "27" in X" — uses "with" instead of "as"
    (re.compile(r'^we select(?: the)? (.+?) with "([^"]+)"(?: in .+ screen)?$', re.IGNORECASE), "select"),
    # "choose X as Y"  /  "we choose X as Y"
    (re.compile(r'^(?:we )?choose (?:the )?(.+?) (?:as|with) "([^"]+)"(?: in .+ screen)?$', re.IGNORECASE), "select"),
    # "we select "Y" from/in X" — value before field
    (re.compile(r'^we select "([^"]+)" (?:from|in) (?:the )?(.+?)$', re.IGNORECASE), "select_value_first"),

    # ── Check ────────────────────────────────────────────────────────────────
    (re.compile(r'^I check "([^"]*)"$', re.IGNORECASE), "check"),

    # ── Logout — specific before generic click ───────────────────────────────
    (re.compile(r"^I logout$", re.IGNORECASE), "logout"),
    (re.compile(r"^clicks? on logout button$", re.IGNORECASE), "logout"),
    (re.compile(r"^we click on (?:the )?logout button$", re.IGNORECASE), "logout"),

    # ── Click / submit ────────────────────────────────────────────────────────
    (re.compile(r'^I click "([^"]*)"$', re.IGNORECASE), "click"),
    (re.compile(r"^click on the (.+?) button(?: in .+ screen)?$", re.IGNORECASE), "click"),
    (re.compile(r"^click on submit button(?: in .+ screen)?$", re.IGNORECASE), "click_submit"),
    # "click on submit of fee collection screen" / "click on submit of X" — unusual phrasing
    (re.compile(r"^click on submit\b.*$", re.IGNORECASE), "click_submit"),
    (re.compile(r"^we click on (?:the )?submit button(?: in .+ screen)?$", re.IGNORECASE), "click_submit"),
    (re.compile(r"^we click on (?:the )?(.+?) button(?: in .+ screen)?$", re.IGNORECASE), "click"),
    # "we click submit button in X" / "we click the submit button in X" — missing "on"
    (re.compile(r"^we click(?: the)? submit button(?: in .+ screen)?$", re.IGNORECASE), "click_submit"),
    # "we click the X button in Y" / "we click X button in Y" — generic, missing "on"
    (re.compile(r"^we click(?: the)? (.+?) button(?: in .+ screen)?$", re.IGNORECASE), "click"),
    # "click on the refresh button and get balance from cash drawer"
    (re.compile(r"^click on the refresh button\b.*$", re.IGNORECASE), "click_refresh"),
    # "retrieve the journal number from alert and click on ok"
    (re.compile(r"^retrieve the journal number from alert and click on ok$", re.IGNORECASE), "dismiss_ok"),

    # ── Approve / authorize ──────────────────────────────────────────────────
    (re.compile(r"^I approve the transaction$", re.IGNORECASE), "approve"),
    (re.compile(r"^authorize the transaction\b.*$", re.IGNORECASE), "approve"),
    (re.compile(r"^we searched? for journal number and authori[zs]ed?$", re.IGNORECASE), "approve"),
    (re.compile(r"^click on EJ and authori[zs]e the transaction\b.*$", re.IGNORECASE), "approve"),
    # "authorize the transaction using "checkerID" and "checkerPassword"" — inline checker auth
    (re.compile(r'^authorize the transaction using "([^"]*)" and "([^"]*)"$', re.IGNORECASE), "login_then_approve"),

    # ── Submit for approval ───────────────────────────────────────────────────
    (re.compile(r"^I submit for approval$", re.IGNORECASE), "submit_for_approval"),

    # ── Assert visible ────────────────────────────────────────────────────────
    (re.compile(r'^I should see "([^"]*)"$', re.IGNORECASE), "assert_visible"),

    # ── Dismiss popups / alerts ───────────────────────────────────────────────
    (re.compile(r"^we accept supervisor override success alert$", re.IGNORECASE), "dismiss_ok"),
    # Cash denomination screen — best-effort click Submit; the full cash-drawer
    # popup cannot be replicated without a real denomination locator model.
    (re.compile(r"^fill the cash denomination details\b.*$", re.IGNORECASE), "click_submit"),

    # ── Last-resort catch-all fill ────────────────────────────────────────────
    # Matches any step that contains a field phrase followed by 'as "value"',
    # regardless of the surrounding wording. This catches novel phrasings that
    # weren't explicitly enumerated above (e.g. "enter the FCRA account as X").
    # Placed AFTER all specific action patterns and BEFORE the assertion-skip block
    # so it only fires when nothing else has matched.
    (re.compile(r'^.+? (?:the )?(.+?) as "([^"]+)".*$', re.IGNORECASE), "fill_catchall"),

    # ── Value-first fill ──────────────────────────────────────────────────────
    # Some NBC feature files write the quoted value BEFORE the field name:
    #   "we enter "myfirstNamefirst" name in Capture FCRA Cash Donor Details screen"
    # The fill patterns above all expect  field as "value"  — this pattern
    # covers the reversed  "value" fieldname  format so those steps are executed
    # rather than silently skipped by the skip_no_value rule below.
    (re.compile(r'^we enter "([^"]+)" (.+?) in .+ screen$', re.IGNORECASE), "fill_value_first"),

    # ── Assertion / verification / collection — skip gracefully ───────────────
    # These steps require the Java framework's HashMap data model, ExtentReports
    # assertions, or DB queries — none of which exist in this platform. Skipping
    # them lets the surrounding fill/click steps execute normally.
    (re.compile(r"^(?:check|verify|assert|confirm|validate)\b.+$", re.IGNORECASE), "skip_assertion"),
    (re.compile(r"^(?:collect|retrieve|gather|get|fetch)\b.+$", re.IGNORECASE), "skip_collection"),
    (re.compile(r"^since pan card is present\b.+$", re.IGNORECASE), "skip_complex"),
    (re.compile(r"^(?:verify|check)\s+(?:the\s+)?\"[^\"]+\"\b.+$", re.IGNORECASE), "skip_assertion"),
    # "we fill Form60 details …" — multi-arg complex step, skip
    (re.compile(r"^we fill Form60 details\b.+$", re.IGNORECASE), "skip_complex"),
    # "we validate X and Y details …"
    (re.compile(r"^we validate\b.+$", re.IGNORECASE), "skip_assertion"),
    # "we get journal number …" — data collection
    (re.compile(r"^we get\b.+$", re.IGNORECASE), "skip_collection"),
    # "X should be Y" / "X should display Y" / "result should …"
    (re.compile(r"^.+\bshould\b.+$", re.IGNORECASE), "skip_assertion"),
    # "make sure X" / "ensure X"
    (re.compile(r"^(?:make sure|ensure)\b.+$", re.IGNORECASE), "skip_assertion"),
    # "we enter X in Y screen" with no quoted value → can't resolve without value
    (re.compile(r"^we enter .+ in .+ screen$", re.IGNORECASE), "skip_no_value"),
    # "click on EJ" (without "and authorize") — ambiguous, skip
    (re.compile(r"^click on EJ\b.*$", re.IGNORECASE), "skip_complex"),
]


def _parse_step_text(text: str) -> Step:
    for pattern, kind in _PATTERNS:
        match = pattern.match(text.strip())
        if not match:
            continue
        groups = match.groups()

        if kind == "login":
            return LoginStep(role=groups[0].lower(), username=groups[1], password=groups[2], raw=text)

        if kind == "login_alias":
            role = "checker" if "checker" in groups[0].lower() else "maker"
            return LoginStep(role=role, username=groups[0], password=groups[1], raw=text)

        if kind == "login_then_approve":
            # "authorize the transaction using "checkerID" and "checkerPassword"" —
            # treat as a checker login; the implicit ApproveStep that follows in the
            # Java flow is not emitted here (would require returning two steps).
            # The platform will log in as checker; the user should add an explicit
            # authorize step in their feature file for the approval click.
            role = "checker" if "checker" in groups[0].lower() else "maker"
            return LoginStep(role=role, username=groups[0], password=groups[1], raw=text)

        if kind == "search":
            return SearchStep(query=groups[0], raw=text)

        if kind == "short_enquiry":
            return ShortEnquiryStep(account_number=groups[0], raw=text)

        if kind == "fill":
            return FillStep(field_name=groups[0], value=groups[1], raw=text)

        if kind == "fill_catchall":
            # Best-effort: the last group is always the quoted value; the
            # field_name is whatever came before "as".
            return FillStep(field_name=groups[0].strip(), value=groups[1], raw=text)

        if kind == "fill_value_first":
            # Value comes before the field name: we enter "VALUE" FIELD in X screen
            return FillStep(field_name=groups[1].strip(), value=groups[0], raw=text)

        if kind == "select":
            return SelectStep(field_name=groups[0], value=groups[1], raw=text)

        if kind == "select_value_first":
            # Value comes before the field name: we select "VALUE" from/in FIELD
            return SelectStep(field_name=groups[1].strip(), value=groups[0], raw=text)

        if kind == "check":
            return CheckStep(field_name=groups[0], raw=text)

        if kind == "click":
            return ClickStep(button_text=groups[0], raw=text)

        if kind == "click_submit":
            return ClickStep(button_text="Submit", raw=text)

        if kind == "click_refresh":
            return ClickStep(button_text="Refresh", raw=text)

        if kind == "dismiss_ok":
            return ClickStep(button_text="OK", raw=text)

        if kind == "submit_for_approval":
            return SubmitForApprovalStep(raw=text)

        if kind == "approve":
            return ApproveStep(raw=text)

        if kind == "logout":
            return LogoutStep(raw=text)

        if kind == "assert_visible":
            return AssertVisibleStep(field_name=groups[0], raw=text)

        if kind in ("skip_assertion", "skip_collection", "skip_complex", "skip_no_value"):
            reason_map = {
                "skip_assertion": "assertion/verification steps are not executed by this platform",
                "skip_collection": "data-collection steps require the Java framework's data model",
                "skip_complex": "complex multi-step operation not yet supported",
                "skip_no_value": "fill step has no quoted value to enter",
            }
            return SkippedStep(reason=reason_map[kind], raw=text)

    # ── Phase 2: keyword inference (no API cost) ──────────────────────────────
    inferred = _infer_step_from_keywords(text)
    if inferred is not None:
        return inferred

    # ── Phase 3: LLM classifier (lazy import, cached, null-safe) ─────────────
    try:
        from app.agents.step_classifier_agent import classify_step
        classification = classify_step(text)
        if classification:
            return _build_step_from_classification(classification, text)
    except Exception:
        pass

    return UnrecognizedStep(raw=text)


# ---------------------------------------------------------------------------
# Keyword inference helpers (Phase 2 fallback)
# ---------------------------------------------------------------------------

_STOPWORDS_FIELD = frozenset({
    "we", "i", "enter", "fill", "type", "input", "the", "a", "an",
    "into", "as", "with", "for", "in", "on", "and", "then", "also",
    "please", "click", "press", "select", "choose", "from", "of",
})


def _extract_field_name_heuristic(text: str, value: str) -> str:
    """Strip the quoted value and boilerplate words, leaving only field-name words."""
    clean = text.replace(f'"{value}"', " ")
    clean = re.sub(r"\s+(?:in|on)\s+\w[\w\s]*\s+screen\s*$", " ", clean, flags=re.IGNORECASE)
    words = clean.strip().split()
    field_words = [w for w in words if w.lower().rstrip(".,;") not in _STOPWORDS_FIELD and len(w) > 1]
    return " ".join(field_words).strip() or value


def _extract_button_text_heuristic(text: str) -> str:
    """Extract the most likely button label from a click/press step."""
    m = re.search(
        r"\b(?:click|press|tap|hit)\s+(?:on\s+)?(?:the\s+)?(.+?)(?:\s*button\b.*)?$",
        text, re.IGNORECASE,
    )
    if m:
        btn = m.group(1).strip()
        btn = re.sub(r"\s*\bin\s+\w[\w\s]*\s*screen\s*$", "", btn, flags=re.IGNORECASE)
        btn = re.sub(r"\s*\bbutton\s*$", "", btn, flags=re.IGNORECASE)
        btn = btn.strip()
        if btn:
            return btn
    return "Submit"


def _infer_step_from_keywords(text: str) -> "Step | None":
    """Heuristic intent-inference when regex patterns all fail.

    Checks keyword presence to classify the most common patterns that don't fit
    the explicit regex list.  Returns None when inference is too ambiguous so the
    LLM classifier (Phase 3) gets a chance.
    """
    lower = text.strip().lower()
    quoted = re.findall(r'"([^"]*)"', text)

    # Logout — check before any skip heuristic
    if any(kw in lower for kw in ("logout", "log out", "sign out", "logoff", "log off")):
        return LogoutStep(raw=text)

    # Approve/Authorize — but not "logged in as" steps
    if any(kw in lower for kw in ("authoriz", "authoris", "approv")) and "logged" not in lower:
        return ApproveStep(raw=text)

    # Pure assertion / visibility / state checks with no action keyword
    _assertion_kw = ("should", "must", "ensure", "make sure", "display", "shown",
                     "visible", "appears", "is present", "is displayed")
    _action_kw = ("click", "enter", "fill", "type", "select", "input")
    if any(kw in lower for kw in _assertion_kw) and not any(kw in lower for kw in _action_kw):
        return SkippedStep(reason="assertion/state-check inferred from step wording", raw=text)

    # Fill: enter / fill / type / input / provide / write + a quoted value
    if any(kw in lower for kw in ("enter", "fill", "type", "input", "provide", "write")) and quoted:
        value = quoted[0]
        field = _extract_field_name_heuristic(text, value)
        return FillStep(field_name=field, value=value, raw=text)

    # Select: select / choose / pick + a quoted value
    if any(kw in lower for kw in ("select", "choose", "pick")) and quoted:
        value = quoted[0]
        field = _extract_field_name_heuristic(text, value)
        return SelectStep(field_name=field, value=value, raw=text)

    # Click: click / press / tap — even without a quoted value
    if any(kw in lower for kw in ("click", "press", "tap")):
        return ClickStep(button_text=_extract_button_text_heuristic(text), raw=text)

    return None


# ---------------------------------------------------------------------------
# LLM classification result → Step (Phase 3 fallback)
# ---------------------------------------------------------------------------

def _build_step_from_classification(c: dict, raw: str) -> "Step | None":
    """Convert the dict returned by step_classifier_agent.classify_step() into a Step."""
    t = (c.get("type") or "skip").lower()
    if t == "fill":
        field = (c.get("field_name") or "").strip()
        value = (c.get("value") or "").strip()
        if field and value:
            return FillStep(field_name=field, value=value, raw=raw)
    if t == "click":
        btn = (c.get("button_text") or "Submit").strip()
        return ClickStep(button_text=btn, raw=raw)
    if t == "select":
        field = (c.get("field_name") or "").strip()
        value = (c.get("value") or "").strip()
        if field and value:
            return SelectStep(field_name=field, value=value, raw=raw)
    if t == "login":
        uname = (c.get("username") or "").strip()
        pwd = (c.get("password") or "").strip()
        role = "checker" if "checker" in uname.lower() else "maker"
        return LoginStep(role=role, username=uname, password=pwd, raw=raw)
    if t == "search":
        q = (c.get("query") or "").strip()
        if q:
            return SearchStep(query=q, raw=raw)
    if t == "approve":
        return ApproveStep(raw=raw)
    if t == "logout":
        return LogoutStep(raw=raw)
    # Default: skip (includes "skip" type and any unrecognized type from LLM)
    reason = (c.get("reason") or "classified as non-executable by LLM").strip()
    return SkippedStep(reason=reason, raw=raw)


def parse_steps(raw_feature_text: str) -> list[Step]:
    steps: list[Step] = []
    for line in raw_feature_text.splitlines():
        match = _STEP_LINE.match(line)
        if not match:
            continue
        steps.append(_parse_step_text(match.group(1)))
    return steps
