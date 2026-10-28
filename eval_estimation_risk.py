# eval_estimation_risk.py

"""Smoke tests for the Estimation & Risk Agent."""

from __future__ import annotations

import json

from agents.estimation_risk import (
    EstimationRiskAgent,
    calculate_pert,
)


MOCK_LLM_RESPONSE = {
    "work_item_estimates": [
        {
            "work_item_id": "KAN-101",
            "title": "Implement authentication endpoint",
            "optimistic_hours": 8,
            "most_likely_hours": 12,
            "pessimistic_hours": 20,
            "complexity": "medium",
            "assumptions": [
                "Existing identity provider can be reused."
            ],
            "dependencies": ["KAN-099"],
            "confidence": "medium",
            "needs_review": False,
            "review_reasons": [],
        },
        {
            "work_item_id": "KAN-102",
            "title": "Add integration tests",
            "optimistic_hours": 4,
            "most_likely_hours": 8,
            "pessimistic_hours": 16,
            "complexity": "medium",
            "assumptions": [
                "A test environment will be available."
            ],
            "dependencies": ["KAN-101"],
            "confidence": "medium",
            "needs_review": True,
            "review_reasons": [
                "Test environment availability is unconfirmed."
            ],
        },
    ],
    "risk_register": [
        {
            "risk_id": "RISK-001",
            "category": "dependency",
            "description": (
                "Integration testing may be delayed if the "
                "identity provider is unavailable."
            ),
            "likelihood": "medium",
            "impact": "high",
            "evidence": (
                "Authentication depends on an existing identity provider."
            ),
            "mitigation": (
                "Confirm access and provide a mock or test environment."
            ),
            "affected_work_items": ["KAN-101", "KAN-102"],
            "owner": None,
        }
    ],
    "overall_confidence": "medium",
    "project_assumptions": [
        "Existing authentication infrastructure is available."
    ],
    "open_questions": [
        "When will the integration test environment be available?"
    ],
}


def mock_llm_json_call(
    system: str,
    user: str,
    max_tokens: int = 3000,
    attempts: int = 3,
    validate=None,
):
    assert "Estimation" in system
    assert (
        "INPUT:" in user
        or "PROJECT SUMMARY:" in user
    ), f"Unexpected prompt format: {user[:300]}"

    if "PROJECT SUMMARY:" in user:
        result = {
            "work_item_estimates": [],
            "risk_register": MOCK_LLM_RESPONSE["risk_register"],
            "overall_confidence": MOCK_LLM_RESPONSE["overall_confidence"],
            "project_assumptions": MOCK_LLM_RESPONSE["project_assumptions"],
            "open_questions": MOCK_LLM_RESPONSE["open_questions"],
        }
        return validate(result) if validate else result

    # Extract the work-item ID from the JSON input section.
    marker = "INPUT:"
    if marker not in user:
        raise AssertionError(
            f"Could not find INPUT section in prompt: {user[:500]}"
        )

    input_json = user.split(marker, 1)[1].strip()
    input_data = json.loads(input_json)

    items = input_data.get("work_items_for_estimation", [])

    if len(items) != 1:
        raise AssertionError(
            f"Expected one input work item; found {len(items)}"
        )

    requested_id = items[0]["work_item_id"]

    # Return only the estimate for the requested story.
    matching_estimates = [
        item
        for item in MOCK_LLM_RESPONSE["work_item_estimates"]
        if item["work_item_id"] == requested_id
    ]

    if len(matching_estimates) != 1:
        raise AssertionError(
            f"Expected one mock estimate for {requested_id}; "
            f"found {len(matching_estimates)}"
        )

    result = {
        "work_item_estimates": matching_estimates,
        "risk_register": [],
        "overall_confidence": MOCK_LLM_RESPONSE["overall_confidence"],
        "project_assumptions": [],
        "open_questions": [],
    }

    return validate(result) if validate else result


def test_pert_calculation() -> None:
    result = calculate_pert(8, 12, 20)

    assert result["low_hours"] == 8.0
    assert result["most_likely_hours"] == 12.0
    assert result["expected_hours"] == 12.67
    assert result["high_hours"] == 20.0


def test_agent_rollup() -> None:
    agent = EstimationRiskAgent(
        llm_call=mock_llm_json_call
    )

    result = agent.run({
        "project_id": "KAN",
        "stories": [
            {
                "work_item_id": "KAN-101",
                "title": "Implement authentication endpoint",
            },
            {
                "work_item_id": "KAN-102",
                "title": "Add integration tests",
            },
        ],
    })

    assert result["artifact_type"] == "estimation_risk"
    assert result["estimate_unit"] == "person-hours"
    assert len(result["work_item_estimates"]) == 2
    assert len(result["risk_register"]) == 1

    # Expected effort:
    # KAN-101: (8 + 4*12 + 20) / 6 = 12.67
    # KAN-102: (4 + 4*8 + 16) / 6 = 8.00
    assert result["project_effort"]["expected_hours"] == 21.34
    assert result["project_effort"]["low_hours"] == 12.0
    assert result["project_effort"]["high_hours"] == 36.0

    assert result["needs_review"] is True
    assert result["schedule"]["calendar_duration_estimated"] is False


def test_duplicate_work_item_rejected() -> None:
    from agents.estimation_risk.schemas import (
        EstimationValidationError,
        validate_llm_payload,
    )

    invalid = dict(MOCK_LLM_RESPONSE)
    invalid["work_item_estimates"] = [
        MOCK_LLM_RESPONSE["work_item_estimates"][0],
        MOCK_LLM_RESPONSE["work_item_estimates"][0],
    ]

    try:
        validate_llm_payload(invalid)
    except EstimationValidationError:
        pass
    else:
        raise AssertionError("Duplicate work item IDs should be rejected.")


if __name__ == "__main__":
    test_pert_calculation()
    test_agent_rollup()
    test_duplicate_work_item_rejected()

    print("All Estimation & Risk Agent smoke tests passed.")
