# agents/estimation_risk/schemas.py

"""Output contract and validation for the Estimation & Risk Agent."""

from __future__ import annotations
from typing import Any

RISK_CATEGORIES = {
    "technical",
    "delivery",
    "dependency",
    "schedule",
}

LIKELIHOOD_LEVELS = {"low", "medium", "high"}
IMPACT_LEVELS = {"low", "medium", "high", "critical"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}

REQUIRED_STORY_FIELDS = {
    "work_item_id",
    "title",
    "optimistic_hours",
    "most_likely_hours",
    "pessimistic_hours",
    "complexity",
    "assumptions",
    "dependencies",
    "confidence",
    "needs_review",
}

REQUIRED_RISK_FIELDS = {
    "risk_id",
    "category",
    "description",
    "likelihood",
    "impact",
    "evidence",
    "mitigation",
}


class EstimationValidationError(ValueError):
    """Raised when the estimation output violates the contract."""

def _require_non_empty_string(value: Any, field: str,) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EstimationValidationError(
            f"'{field}' must be a non-empty string."
        )
    return value.strip()


def _require_non_negative_number(value: Any, field: str,) -> float:
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EstimationValidationError(
            f"'{field}' must be a number."
        )

    number = float(value)

    if number < 0:
        raise EstimationValidationError(f"'{field}' cannot be negative.")

    return number


def _require_string_list(value: Any, field: str,) -> list[str]:
    if not isinstance(value, list):
        raise EstimationValidationError(
            f"'{field}' must be a list of strings."
        )

    cleaned = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise EstimationValidationError(
                f"'{field}[{index}]' must be a non-empty string."
            )
        cleaned.append(item.strip())

    return cleaned


def validate_story(story: Any) -> dict[str, Any]:
    """Validate one work-item estimate and return a normalized copy."""

    if not isinstance(story, dict):
        raise EstimationValidationError(
            "Each story estimate must be a JSON object."
        )

    missing = REQUIRED_STORY_FIELDS - story.keys()
    if missing:
        raise EstimationValidationError(
            f"Story is missing fields: {sorted(missing)}"
        )

    result = dict(story)

    result["work_item_id"] = _require_non_empty_string(
        story["work_item_id"], "work_item_id"
    )
    result["title"] = _require_non_empty_string(
        story["title"], "title"
    )

    result["optimistic_hours"] = _require_non_negative_number(
        story["optimistic_hours"], "optimistic_hours"
    )
    result["most_likely_hours"] = _require_non_negative_number(
        story["most_likely_hours"], "most_likely_hours"
    )
    result["pessimistic_hours"] = _require_non_negative_number(
        story["pessimistic_hours"], "pessimistic_hours"
    )

    optimistic = result["optimistic_hours"]
    most_likely = result["most_likely_hours"]
    pessimistic = result["pessimistic_hours"]

    if not optimistic <= most_likely <= pessimistic:
        raise EstimationValidationError(
            f"Invalid estimate range for '{result['work_item_id']}': "
            "optimistic_hours <= most_likely_hours <= "
            "pessimistic_hours must hold."
        )

    result["complexity"] = _require_non_empty_string(
        story["complexity"], "complexity"
    ).lower()

    if result["complexity"] not in {
        "low", "medium", "high", "unknown"
    }:
        raise EstimationValidationError(
            f"Invalid complexity for '{result['work_item_id']}'."
        )

    result["assumptions"] = _require_string_list(
        story["assumptions"], "assumptions"
    )
    result["dependencies"] = _require_string_list(
        story["dependencies"], "dependencies"
    )

    confidence = _require_non_empty_string(
        story["confidence"], "confidence"
    ).lower()

    if confidence not in CONFIDENCE_LEVELS:
        raise EstimationValidationError(
            f"Invalid confidence for '{result['work_item_id']}'."
        )

    result["confidence"] = confidence

    if not isinstance(story["needs_review"], bool):
        raise EstimationValidationError(
            "'needs_review' must be a boolean."
        )

    result["needs_review"] = story["needs_review"]

    # Keep uncertainty visible instead of silently accepting a zero-width
    # range as evidence of certainty.
    if optimistic == pessimistic:
        result["needs_review"] = True
        result.setdefault("review_reasons", []).append(
            "Estimate range has no uncertainty."
        )

    review_reasons = story.get("review_reasons", [])
    if not isinstance(review_reasons, list) or not all(
        isinstance(reason, str) for reason in review_reasons
    ):
        raise EstimationValidationError(
            "'review_reasons' must be a list of strings."
        )

    result["review_reasons"] = list(
        dict.fromkeys(
            result.get("review_reasons", []) + review_reasons
        )
    )

    return result


def validate_risk(risk: Any) -> dict[str, Any]:
    """Validate one risk-register entry."""

    if not isinstance(risk, dict):
        raise EstimationValidationError(
            "Each risk must be a JSON object."
        )

    missing = REQUIRED_RISK_FIELDS - risk.keys()
    if missing:
        raise EstimationValidationError(
            f"Risk is missing fields: {sorted(missing)}"
        )

    result = dict(risk)

    result["risk_id"] = _require_non_empty_string(
        risk["risk_id"], "risk_id"
    )
    result["description"] = _require_non_empty_string(
        risk["description"], "description"
    )
    result["evidence"] = _require_non_empty_string(
        risk["evidence"], "evidence"
    )
    result["mitigation"] = _require_non_empty_string(
        risk["mitigation"], "mitigation"
    )

    category = _require_non_empty_string(
        risk["category"], "category"
    ).lower()

    if category not in RISK_CATEGORIES:
        raise EstimationValidationError(
            f"Invalid risk category '{category}'."
        )

    result["category"] = category

    likelihood = _require_non_empty_string(
        risk["likelihood"], "likelihood"
    ).lower()

    if likelihood not in LIKELIHOOD_LEVELS:
        raise EstimationValidationError(
            f"Invalid likelihood '{likelihood}'."
        )

    result["likelihood"] = likelihood

    impact = _require_non_empty_string(
        risk["impact"], "impact"
    ).lower()

    if impact not in IMPACT_LEVELS:
        raise EstimationValidationError(
            f"Invalid impact '{impact}'."
        )

    result["impact"] = impact

    result["affected_work_items"] = _require_string_list(
        risk.get("affected_work_items", []),
        "affected_work_items",
    )

    result["owner"] = risk.get("owner")
    if result["owner"] is not None and not isinstance(
        result["owner"], str
    ):
        raise EstimationValidationError(
            "'owner' must be a string or null."
        )

    return result


def validate_llm_payload(payload: Any) -> dict[str, Any]:
    """Validate and normalize the full JSON response from the LLM."""

    if not isinstance(payload, dict):
        raise EstimationValidationError(
            "The LLM response must be a JSON object."
        )

    if not isinstance(payload.get("work_item_estimates"), list):
        raise EstimationValidationError(
            "'work_item_estimates' must be a list."
        )

    if not isinstance(payload.get("risk_register"), list):
        raise EstimationValidationError(
            "'risk_register' must be a list."
        )

    estimates = [
        validate_story(item)
        for item in payload["work_item_estimates"]
    ]
    risks = [
        validate_risk(item)
        for item in payload["risk_register"]
    ]

    ids = [item["work_item_id"] for item in estimates]
    if len(ids) != len(set(ids)):
        raise EstimationValidationError(
            "Duplicate work_item_id values found."
        )

    risk_ids = [item["risk_id"] for item in risks]
    if len(risk_ids) != len(set(risk_ids)):
        raise EstimationValidationError(
            "Duplicate risk_id values found."
        )

    project_confidence = str(
        payload.get("overall_confidence", "low")
    ).lower()

    if project_confidence not in CONFIDENCE_LEVELS:
        raise EstimationValidationError(
            "'overall_confidence' must be low, medium, or high."
        )

    assumptions = _require_string_list(
        payload.get("project_assumptions", []),
        "project_assumptions",
    )
    open_questions = _require_string_list(
        payload.get("open_questions", []),
        "open_questions",
    )

    return {
        "work_item_estimates": estimates,
        "risk_register": risks,
        "overall_confidence": project_confidence,
        "project_assumptions": assumptions,
        "open_questions": open_questions,
    }
