"""Step Executor.

Executes a single parsed step (see `feature_step_parser`) against a live Selenium
session: resolves the target field via `locator_resolver` (Locator Repository, with
lightweight self-healing fallback), performs the Selenium action with smart waits,
and reports back what locator was used / whether healing occurred so the caller can
persist an `ExecutionStep` (and `HealingHistory`) row.
"""

from dataclasses import dataclass

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait
from sqlalchemy.orm import Session

from app.agents.feature_step_parser import (
    ApproveStep,
    AssertVisibleStep,
    CheckStep,
    ClickStep,
    FillStep,
    LoginStep,
    LogoutStep,
    SearchStep,
    SelectStep,
    ShortEnquiryStep,
    SkippedStep,
    Step,
    SubmitForApprovalStep,
    UnrecognizedStep,
)
from app.agents.locator_resolver import ElementResolutionError, resolve_element
from app.agents.login_agent import perform_login
from app.agents.smart_wait import wait_until_clickable, wait_for_document_ready
from app.agents.transaction_search_agent import search_transaction
from app.core.config import get_settings

_LOGOUT_HINTS = ["logout", "log out", "sign out"]

_CREDENTIAL_PLACEHOLDERS = {
    "makerid": "maker_username",
    "makerusername": "maker_username",
    "makerpassword": "maker_password",
    "checkerid": "checker_username",
    "checkerusername": "checker_username",
    "checkerpassword": "checker_password",
    "username": "default_username",
    "userid": "default_username",
    "password": "default_password",
}


def _resolve_credential(value: str) -> str:
    """Feature files (matching the user's existing Java/Cucumber convention) often
    write placeholder tokens like "makerID"/"makerPassword" instead of literal
    credentials — those get resolved from the backend credential vault here. Anything
    that isn't a recognized placeholder is treated as a literal credential, so
    hand-typed real values keep working exactly as before."""
    settings_field = _CREDENTIAL_PLACEHOLDERS.get(value.strip().lower())
    if settings_field:
        resolved = getattr(get_settings(), settings_field, "")
        if resolved:
            return resolved
    return value


@dataclass
class StepOutcome:
    status: str  # "passed" | "failed"
    locator_used: str | None = None
    locator_type: str | None = None
    healed: bool = False
    healing_info: dict | None = None
    error: Exception | None = None


def _find_button_by_text(driver: WebDriver, text: str) -> WebElement | None:
    needle = text.strip().lower()
    elements = driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit'], input[type='button'], a")
    for el in elements:
        if not el.is_displayed():
            continue
        candidate = (el.text or el.get_attribute("value") or "").strip().lower()
        if needle in candidate or candidate in needle:
            return el
    return None


# Real NBC screens raise interstitial popups mid-flow that don't correspond to any
# feature-file step: an "Applicable charge is Rs. X" information alert after Submit,
# a "Customer Details" KYC-status popup after an Account Number lookup. Both are
# acknowledge-only — there's no business decision being made, just dismissing a
# notice — so they're safe to auto-dismiss. Deliberately NARROW and exact-match only:
# "Close", "Cancel", "Yes", "No", "Send to Supervisor" etc. are real business actions
# that stay step-driven, never auto-clicked.
_POPUP_DISMISS_TEXTS = {"ok", "update later"}


def _dismiss_known_popups(driver: WebDriver, max_dismissals: int = 3) -> None:
    for _ in range(max_dismissals):
        elements = driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit'], input[type='button'], a")
        dismissed = False
        for el in elements:
            if not el.is_displayed() or not el.is_enabled():
                continue
            text = (el.text or el.get_attribute("value") or "").strip().lower()
            if text in _POPUP_DISMISS_TEXTS:
                try:
                    el.click()
                except Exception:
                    continue
                dismissed = True
                break
        if not dismissed:
            return


def execute_step(driver: WebDriver, db: Session, step: Step, context: dict) -> StepOutcome:
    try:
        # Every step starts from the top-level document. `resolve_element` switches
        # into an iframe when a field lives there and leaves the driver positioned
        # inside it; without this reset, the next step (e.g. clicking "Next" in the
        # parent document) would be searched for inside that stale iframe context.
        driver.switch_to.default_content()

        if isinstance(step, LoginStep):
            wait_for_document_ready(driver)
            username = _resolve_credential(step.username)
            password = _resolve_credential(step.password)
            success = perform_login(driver, driver.current_url, username, password)
            if not success:
                raise ElementResolutionError(
                    "Login", "no username/password field found on the current page — already logged in?"
                )
            context["role"] = step.role
            # After a successful login the NBC SPA loads the home dashboard
            # asynchronously. Wait up to 15 s for the transaction search field
            # to become available so the very next SearchStep doesn't fire
            # before the dashboard is ready.
            try:
                from app.agents.transaction_search_agent import find_search_field
                WebDriverWait(driver, 15).until(lambda d: find_search_field(d) is not None)
            except Exception:
                pass  # best-effort; don't fail the login step itself
            return StepOutcome("passed")

        if isinstance(step, SearchStep):
            found = search_transaction(driver, step.query, None)
            if not found:
                raise ValueError(
                    f"Transaction search field not found — could not search for '{step.query}'. "
                    "The home dashboard may not have loaded or the search box was not detected."
                )
            context["transaction_number"] = step.query
            return StepOutcome("passed")

        if isinstance(step, FillStep):
            resolved = resolve_element(driver, db, context.get("transaction_number"), step.field_name)
            wait_until_clickable(resolved.element, driver)
            resolved.element.clear()
            resolved.element.send_keys(step.value)
            # Several real NBC fields (e.g. Account Number) only run their lookup —
            # populating Name, sometimes raising a KYC popup — on blur/Tab, not on
            # every keystroke. Tabbing out mirrors what a human tester actually does.
            resolved.element.send_keys(Keys.TAB)
            _dismiss_known_popups(driver)
            return StepOutcome("passed", resolved.locator_used, resolved.locator_type, resolved.healed, resolved.healing_info)

        if isinstance(step, ShortEnquiryStep):
            # Fill the Account Number field, tab to trigger the account lookup,
            # dismiss any KYC/customer-details popup, then click Submit so the
            # account details are displayed before the next step runs.
            resolved = resolve_element(driver, db, context.get("transaction_number"), "Account Number")
            wait_until_clickable(resolved.element, driver)
            resolved.element.clear()
            resolved.element.send_keys(step.account_number)
            resolved.element.send_keys(Keys.TAB)
            _dismiss_known_popups(driver)
            submit_el = _find_button_by_text(driver, "submit")
            if submit_el is not None:
                wait_until_clickable(submit_el, driver)
                submit_el.click()
                wait_for_document_ready(driver)
                _dismiss_known_popups(driver)
            return StepOutcome("passed", resolved.locator_used, resolved.locator_type, resolved.healed, resolved.healing_info)

        if isinstance(step, SelectStep):
            # `field_name` is a <select> dropdown's own label in our locator builder,
            # but for radio-button-style groups it's just the group's caption text —
            # there is no element for "Transfer Mode" itself, only for each option
            # ("NEFT", "RTGS", ...). Try the dropdown path first; fall back to
            # resolving the option value directly when the field name itself doesn't
            # resolve to anything.
            try:
                resolved = resolve_element(driver, db, context.get("transaction_number"), step.field_name)
            except ElementResolutionError:
                resolved = None

            if resolved is not None and resolved.element.tag_name.lower() == "select":
                wait_until_clickable(resolved.element, driver)
                Select(resolved.element).select_by_visible_text(step.value)
                _dismiss_known_popups(driver)
                return StepOutcome("passed", resolved.locator_used, resolved.locator_type, resolved.healed, resolved.healing_info)

            option = resolve_element(driver, db, context.get("transaction_number"), step.value)
            wait_until_clickable(option.element, driver)
            option.element.click()
            _dismiss_known_popups(driver)
            return StepOutcome("passed", option.locator_used, option.locator_type, option.healed, option.healing_info)

        if isinstance(step, CheckStep):
            resolved = resolve_element(driver, db, context.get("transaction_number"), step.field_name)
            wait_until_clickable(resolved.element, driver)
            if not resolved.element.is_selected():
                resolved.element.click()
            _dismiss_known_popups(driver)
            return StepOutcome("passed", resolved.locator_used, resolved.locator_type, resolved.healed, resolved.healing_info)

        if isinstance(step, ClickStep):
            element = _find_button_by_text(driver, step.button_text)
            if element is None:
                raise ElementResolutionError(step.button_text, "no button/link matched this text")
            wait_until_clickable(element, driver)
            element.click()
            wait_for_document_ready(driver)
            _dismiss_known_popups(driver)
            return StepOutcome("passed", locator_used=f"text={step.button_text}", locator_type="text")

        if isinstance(step, SubmitForApprovalStep):
            element = _find_button_by_text(driver, "submit for approval") or _find_button_by_text(driver, "submit")
            if element is None:
                raise ElementResolutionError("Submit for Approval", "no matching button found")
            wait_until_clickable(element, driver)
            element.click()
            # This button submits a real form POST + redirect — without waiting for
            # the resulting navigation, a `driver.quit()` immediately after the last
            # step in a feature file can race ahead of the request actually landing.
            wait_for_document_ready(driver)
            _dismiss_known_popups(driver)
            return StepOutcome("passed", locator_used="text=Submit for Approval", locator_type="text")

        if isinstance(step, ApproveStep):
            element = _find_button_by_text(driver, "approve")
            if element is None:
                raise ElementResolutionError("Approve", "no matching button found")
            wait_until_clickable(element, driver)
            element.click()
            wait_for_document_ready(driver)
            _dismiss_known_popups(driver)
            return StepOutcome("passed", locator_used="text=Approve", locator_type="text")

        if isinstance(step, LogoutStep):
            element = None
            for hint in _LOGOUT_HINTS:
                element = _find_button_by_text(driver, hint)
                if element:
                    break
            if element is not None:
                element.click()
            else:
                driver.delete_all_cookies()
            return StepOutcome("passed")

        if isinstance(step, AssertVisibleStep):
            resolved = resolve_element(driver, db, context.get("transaction_number"), step.field_name)
            if not resolved.element.is_displayed():
                raise AssertionError(f"Field '{step.field_name}' was found but is not visible")
            return StepOutcome("passed", resolved.locator_used, resolved.locator_type, resolved.healed, resolved.healing_info)

        if isinstance(step, SkippedStep):
            # Assertion/verification/collection steps that the platform cannot
            # execute are skipped gracefully rather than failing the run. The
            # step appears in the execution report with status "skipped" and
            # the reason so the user knows what was encountered.
            return StepOutcome("skipped")

        if isinstance(step, UnrecognizedStep):
            # A step that didn't match any known pattern is skipped with a
            # warning rather than crashing the run. This lets the fill/click
            # steps around it still execute, and the report shows which lines
            # were unrecognized so the user can refine them if needed.
            import logging
            logging.getLogger(__name__).warning("Skipping unrecognized step: %s", step.raw)
            return StepOutcome("skipped")

        raise ValueError(f"No executor implemented for step type {type(step).__name__}")
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure becomes a classified step failure
        return StepOutcome("failed", error=exc)
