"""Transaction Search Agent.

Normalizes transaction numbers so `001060`, `010600`, and `1060` are all treated as
referring to the same transaction (per the spec's explicit example), then searches the
application by number or by name using the same heuristic field-matching style as the
Login Agent.
"""

import logging

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

# The NBC top-nav search box has placeholder text "Enter screen number or part
# of screen name" and the id/name usually contains "screen" or "search". Adding
# those alongside the generic hints lets find_search_field() locate it reliably
# after the home dashboard loads. Keep the list broad so it also works on the
# fixture app and other CBS variants.
_SEARCH_FIELD_HINTS = ["transaction", "txn", "trans", "search", "screen"]

_SEARCH_BUTTON_HINTS = ["search", "find", "go"]

# Common autocomplete/suggestion dropdown selectors used by CBS-style SPAs.
# Checked in order; the first visible match is clicked.
_AUTOCOMPLETE_SELECTORS = (
    "li[role='option'],"
    "ul.dropdown-menu > li > a,"
    "ul.dropdown-menu > li,"
    ".autocomplete-suggestion,"
    ".ui-menu-item > div,"
    ".ui-menu-item"
)


def normalize_transaction_number(raw: str) -> set[str]:
    """Returns the set of plausible string forms for a transaction number so callers
    can match against any zero-padding convention the target app uses."""
    if raw is None:
        return set()
    stripped = raw.strip()
    if not stripped.isdigit():
        return {stripped}
    no_leading_zeros = stripped.lstrip("0") or "0"
    variants = {stripped, no_leading_zeros}
    for width in (4, 6, 8):
        variants.add(no_leading_zeros.zfill(width))
    return variants


def _visible_text_inputs(driver: WebDriver) -> list[WebElement]:
    selector = "input[type='text'], input[type='search'], input:not([type])"
    return [el for el in driver.find_elements(By.CSS_SELECTOR, selector) if el.is_displayed()]


def _matches_hints(el: WebElement, hints: list[str]) -> bool:
    haystack = " ".join(
        filter(
            None,
            [
                el.get_attribute("name") or "",
                el.get_attribute("id") or "",
                el.get_attribute("placeholder") or "",
                el.get_attribute("aria-label") or "",
            ],
        )
    ).lower()
    return any(hint in haystack for hint in hints)


def find_search_field(driver: WebDriver) -> WebElement | None:
    """Return the transaction search input, or None if not yet present.

    Deliberately has NO fallback to candidates[0] — on the NBC home dashboard
    the login form inputs may still be in the DOM (hidden) after a SPA
    transition, and blindly picking the first visible input can select them or
    any other stray field, causing the transaction number to be typed into the
    wrong element.
    """
    candidates = _visible_text_inputs(driver)
    for el in candidates:
        if _matches_hints(el, _SEARCH_FIELD_HINTS):
            return el
    return None


def find_search_button(driver: WebDriver) -> WebElement | None:
    buttons = driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit'], a")
    for el in buttons:
        if not el.is_displayed():
            continue
        text = (el.text or el.get_attribute("value") or "").strip().lower()
        if any(hint in text for hint in _SEARCH_BUTTON_HINTS):
            return el
    return None


def _click_first_autocomplete_suggestion(driver: WebDriver, timeout: float = 2.0) -> bool:
    """Wait up to `timeout` seconds for an autocomplete dropdown, click first item.

    Returns True if a suggestion was clicked, False if none appeared in time.
    """
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: any(
                el.is_displayed()
                for el in d.find_elements(By.CSS_SELECTOR, _AUTOCOMPLETE_SELECTORS)
            )
        )
        for el in driver.find_elements(By.CSS_SELECTOR, _AUTOCOMPLETE_SELECTORS):
            if el.is_displayed():
                el.click()
                return True
    except Exception:
        pass
    return False


def search_transaction(
    driver: WebDriver,
    transaction_number: str | None,
    transaction_name: str | None,
    timeout: int = 20,
) -> bool:
    """Type a transaction number/name into the NBC search box and open the screen.

    Waits up to `timeout` seconds for the search field to appear — necessary
    because the NBC SPA home dashboard finishes rendering asynchronously after
    the login form disappears, and running this too early would find no field.

    Returns True on success, False if the search field could not be located.
    """
    query = transaction_number or transaction_name
    if not query:
        return False

    # Wait for the search field to appear. On the NBC home dashboard this can
    # take a second or two after the login form goes stale.
    try:
        WebDriverWait(driver, timeout).until(lambda d: find_search_field(d) is not None)
    except Exception:
        logger.warning(
            "Timed out waiting for transaction search field (query=%s). "
            "The home dashboard may not have loaded yet.",
            query,
        )
        return False

    field = find_search_field(driver)
    if field is None:
        return False

    field.clear()
    field.send_keys(query)

    # NBC CBS search boxes are typically autocomplete typeaheads: typing the
    # number shows a dropdown of matching transactions. Try to click the first
    # suggestion so the correct screen opens. If no dropdown appears within
    # 2 seconds, fall back to pressing Enter (which also works on most screens).
    clicked = _click_first_autocomplete_suggestion(driver, timeout=2.0)
    if not clicked:
        button = find_search_button(driver)
        if button is not None:
            button.click()
        else:
            field.send_keys(Keys.ENTER)

    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    return True
