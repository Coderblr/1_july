"""Execution Orchestrator.

Runs one or more feature files sequentially ("Feature File 1 -> Feature File 2 ->
... -> Feature File N") against fresh Edge sessions, applying a continue/stop-on-
failure policy. Persistence is deliberately kept out of this module — it calls back
into the supplied hooks after each feature file / step so the service layer can write
DB rows incrementally (so the UI can poll live progress), while this module stays
focused on Selenium orchestration.
"""

import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from app.agents.browser import build_edge_driver
from app.agents.feature_step_parser import expand_scenario_outlines, parse_steps
from app.agents.step_executor import execute_step

StepCallback = Callable[[dict], None]
FeatureFileCallback = Callable[[str, str], None]


def run_execution(
    base_url: str,
    feature_files: list[dict],
    failure_mode: str,
    db: Session,
    screenshot_dir: Path,
    on_feature_file_status: FeatureFileCallback,
    on_step_complete: StepCallback,
    headless: bool | None = None,
) -> dict:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    totals = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "healed": 0}
    sequence = 0
    stop_remaining = False

    for feature_file in feature_files:
        filename = feature_file["filename"]
        if stop_remaining:
            on_feature_file_status(filename, "skipped")
            continue

        on_feature_file_status(filename, "running")
        # Expand Scenario Outline blocks before parsing so that <placeholder>
        # tokens from Examples tables become real values. Feature files that
        # contain only plain Scenarios are passed through unchanged.
        expanded_text = expand_scenario_outlines(feature_file["raw_text"])
        steps = parse_steps(expanded_text)
        context: dict = {}
        driver = build_edge_driver(headless=headless)
        feature_failed = False

        try:
            driver.get(base_url)
            for step in steps:
                sequence += 1
                totals["total"] += 1
                step_screenshot_dir = screenshot_dir / filename.replace("/", "_")
                step_screenshot_dir.mkdir(parents=True, exist_ok=True)

                before_path = step_screenshot_dir / f"{sequence:03d}_before.png"
                _safe_screenshot(driver, before_path)

                start = time.monotonic()
                outcome = execute_step(driver, db, step, context)
                duration_ms = int((time.monotonic() - start) * 1000)

                after_path = step_screenshot_dir / f"{sequence:03d}_after.png"
                _safe_screenshot(driver, after_path)

                if outcome.status == "passed":
                    status = "passed"
                    totals["passed"] += 1
                elif outcome.status == "skipped":
                    status = "skipped"
                    totals["skipped"] += 1
                else:
                    status = "failed"
                    totals["failed"] += 1
                if outcome.healed:
                    totals["healed"] += 1

                on_step_complete(
                    {
                        "feature_filename": filename,
                        "sequence": sequence,
                        "step": step,
                        "status": status,
                        "locator_used": outcome.locator_used,
                        "healed": outcome.healed,
                        "healing_info": outcome.healing_info,
                        "error": outcome.error,
                        "duration_ms": duration_ms,
                        "screenshot_before": str(before_path),
                        "screenshot_after": str(after_path),
                    }
                )

                if status == "failed":
                    feature_failed = True
                    if failure_mode == "stop":
                        stop_remaining = True
                        break
                # "skipped" is not a failure — execution continues regardless
                # of the failure_mode setting.
                    # "continue" and "retry" (retry-next-transaction is equivalent to
                    # continue at the granularity of this phase) both proceed to the
                    # next step within this feature file.
        finally:
            driver.quit()

        on_feature_file_status(filename, "failed" if feature_failed else "success")

    return totals


def _safe_screenshot(driver, path: Path) -> None:
    try:
        driver.save_screenshot(str(path))
    except Exception:
        pass
