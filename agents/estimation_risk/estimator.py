# agents/estimation_risk/estimator.py

"""Deterministic effort calculations and project rollups."""

from __future__ import annotations
from typing import Any


def calculate_pert(
    optimistic_hours: float,
    most_likely_hours: float,
    pessimistic_hours: float,
) -> dict[str, float]:
    """Calculate PERT expected effort and scenario bounds."""

    values = (
        optimistic_hours,
        most_likely_hours,
        pessimistic_hours,
    )

    if any(isinstance(value, bool) for value in values):
        raise ValueError("Effort values must be numeric, not boolean.")

    if any(value < 0 for value in values):
        raise ValueError("Effort values cannot be negative.")

    if not (
        optimistic_hours
        <= most_likely_hours
        <= pessimistic_hours
    ):
        raise ValueError(
            "Expected O <= M <= P for PERT estimation."
        )

    expected = (
        optimistic_hours
        + 4 * most_likely_hours
        + pessimistic_hours
    ) / 6

    return {
        "low_hours": round(float(optimistic_hours), 2),
        "most_likely_hours": round(float(most_likely_hours), 2),
        "expected_hours": round(expected, 2),
        "high_hours": round(float(pessimistic_hours), 2),
    }


def calculate_rollup(
    estimates: list[dict[str, Any]],
) -> dict[str, float]:
    """Sum story-level estimates without applying extra risk buffers."""

    return {
        "low_hours": round(
            sum(item["low_hours"] for item in estimates), 2
        ),
        "most_likely_hours": round(
            sum(item["most_likely_hours"] for item in estimates), 2
        ),
        "expected_hours": round(
            sum(item["expected_hours"] for item in estimates), 2
        ),
        "high_hours": round(
            sum(item["high_hours"] for item in estimates), 2
        ),
    }


def enrich_estimates(
    work_item_estimates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add deterministic PERT fields to validated estimates."""

    enriched = []

    for item in work_item_estimates:
        result = dict(item)
        result.update(
            calculate_pert(
                optimistic_hours=item["optimistic_hours"],
                most_likely_hours=item["most_likely_hours"],
                pessimistic_hours=item["pessimistic_hours"],
            )
        )
        result["estimate_unit"] = "person-hours"
        enriched.append(result)

    return enriched
