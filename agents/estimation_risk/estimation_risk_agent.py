# agents/estimation_risk/estimation_risk_agent.py


"""Main orchestration for the Estimation & Risk Agent."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from agents.common.llm_calls import llm_json
from agents.estimation_risk.estimator import (
    calculate_rollup,
    enrich_estimates,
)
from agents.estimation_risk.prompts import (
    SYSTEM_PROMPT,
    build_estimation_prompt,
)
from agents.estimation_risk.schemas import (
    validate_llm_payload,
)


class EstimationRiskAgent:
    """Estimate work-item effort and identify delivery risks."""

    def __init__(
        self,
        llm_call: Optional[Callable[..., Any]] = None,
        max_tokens: int = 1800,
        attempts: int = 3,
    ) -> None:
        self.llm_call = llm_call or llm_json
        self.max_tokens = max_tokens
        self.attempts = attempts

    def _call_llm(self, prompt: str) -> dict[str, Any]:
        """Call the shared LLM wrapper and validate the JSON response."""
        response = self.llm_call(
            SYSTEM_PROMPT,
            prompt,
            max_tokens=self.max_tokens,
            attempts=self.attempts,
            validate=validate_llm_payload,
        )

        if isinstance(response, str):
            response = json.loads(response)

        return validate_llm_payload(response)

    def run(
        self,
        context_package: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(context_package, dict):
            raise TypeError("context_package must be a dictionary.")

        context = context_package.get("context", context_package)
        work_items = _extract_work_items(context)

        if not work_items:
            raise ValueError(
                "No eligible work items were found in the estimation "
                "context package."
            )

        # Request one: estimate one work item at a time.
        estimates: list[dict[str, Any]] = []

        for item in work_items:
            item_context = {
                "project_id": context.get("project_id"),
                "architecture_baseline": context.get("architecture_baseline"),
                "work_items_for_estimation": [item],
            }

            payload = self._call_llm(
                build_estimation_prompt(item_context)
            )

            expected_id = item["work_item_id"]
            returned = payload["work_item_estimates"]

            if len(returned) != 1:
                raise ValueError(
                    f"Expected exactly one estimate for {expected_id}; "
                    f"received {len(returned)}."
                )

            if returned[0]["work_item_id"] != expected_id:
                raise ValueError(
                    "LLM estimate ID mismatch. "
                    f"Expected {expected_id}, received "
                    f"{returned[0]['work_item_id']}."
                )

            estimates.append(returned[0])

        # Keep project-level context compact. Do not send story bodies,
        # full source metadata, or the original package back to the LLM.
        project_summary = {
            "project_id": context.get("project_id"),
            "architecture_baseline": context.get("architecture_baseline"),
            "summary": item.get("summary", ""),
            "acceptance_criteria": item.get("acceptance_criteria", []),
            "work_items": [
                {
                    "work_item_id": item["work_item_id"],
                    "title": item["title"],
                    "dependencies": item.get("dependencies", []),
                    "open_questions": item.get("open_questions", []),
                    "gaps": item.get("gaps", []),
                    "conflicts": item.get("conflicts", []),
                    "needs_review": estimate["needs_review"],
                    "confidence": estimate["confidence"],
                    "assumptions": estimate["assumptions"],
                }
                for item, estimate in zip(work_items, estimates)
            ],
        }

        # Request two: identify cross-item/project risks using only
        # the compact summary. Work-item estimates are already available.
        risk_prompt = _build_risk_prompt(project_summary)

        risk_payload = self.llm_call(
            SYSTEM_PROMPT,
            risk_prompt,
            max_tokens=1800,
            attempts=3,
            validate=validate_llm_payload,
        )

        if isinstance(risk_payload, str):
            risk_payload = json.loads(risk_payload)

        risk_payload = validate_llm_payload(risk_payload)
        # The risk request must not create new estimates.
        if risk_payload["work_item_estimates"]:
            raise ValueError(
                "Risk assessment unexpectedly returned work-item estimates."
            )

        enriched = enrich_estimates(estimates)
        rollup = calculate_rollup(enriched)

        risks = risk_payload["risk_register"]

        # Deterministic risk IDs prevent duplicate IDs in the combined
        # project risk register.
        for index, risk in enumerate(risks, start=1):
            risk["risk_id"] = f"RISK-{index:03d}"

        review_items = [
            item["work_item_id"]
            for item in enriched
            if item["needs_review"]
        ]

        fingerprint_payload = {
            "context": context,
            "estimates": estimates,
            "risk_register": risks,
        }
        source_fingerprint = _fingerprint(fingerprint_payload)

        return {
            "artifact_type": "estimation_risk",
            "schema_version": "1.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_fingerprint": source_fingerprint,
            "project_id": context.get("project_id"),
            "estimate_unit": "person-hours",
            "estimation_method": "bottom_up_three_point_pert",
            "work_item_estimates": enriched,
            "project_effort": rollup,
            "risk_register": risks,
            "risk_summary": _summarize_risks(risks),
            "overall_confidence": _overall_confidence(enriched),
            "project_assumptions": _deduplicate(
                risk_payload["project_assumptions"]
                + [
                    assumption
                    for item in enriched
                    for assumption in item["assumptions"]
                ]
            ),
            "open_questions": _deduplicate(
                risk_payload["open_questions"]
                + [
                    question
                    for item in work_items
                    for question in item.get("open_questions", [])
                    if isinstance(question, str)
                ]
            ),
            "needs_review": bool(
                review_items
                or risk_payload["open_questions"]
                or any(
                    item["confidence"] == "low"
                    for item in enriched
                )
            ),
            "work_items_requiring_review": review_items,
            "schedule": {
                "calendar_duration_estimated": False,
                "reason": (
                    "A calendar forecast requires team capacity, "
                    "availability, sequencing, and dependency constraints."
                ),
            },
        }



def _build_risk_prompt(summary: dict[str, Any]) -> str:
    """Build a compact, bounded risk-only request."""

    # Limit input size and output complexity while preserving all story IDs.
    
    compact_summary = {
        "project_id": summary.get("project_id"),
        "architecture_baseline": summary.get("architecture_baseline"),
        "work_items": [
            {
                "work_item_id": item["work_item_id"],
                "title": item["title"],
                "summary": item.get("summary", ""),
                "acceptance_criteria": item.get("acceptance_criteria", []),
                "dependencies": item.get("dependencies", [])[:5],
                "open_questions": item.get("open_questions", [])[:5],
                "gaps": item.get("gaps", [])[:5],
                "conflicts": item.get("conflicts", [])[:5],
                "needs_review": item.get("needs_review", False),
                "confidence": item.get("confidence", "low"),
                "assumptions": item.get("assumptions", [])[:5],
            }
            for item in summary.get("work_items", [])
        ],
    }


    serialized = json.dumps(
        compact_summary,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )

    return f"""
Assess the project's most important technical, delivery, dependency,
and schedule risks using only the supplied evidence.

Return valid JSON with exactly these top-level fields:
- work_item_estimates: []
- risk_register
- overall_confidence: low, medium, or high
- project_assumptions: list of strings
- open_questions: list of strings

Return at most 5 distinct risks. Prioritize risks by impact and likelihood.
Keep each description, evidence, and mitigation to one concise sentence.
Do not duplicate risks. Do not invent owners, deadlines, dependencies,
capacity, or facts. Use null for an unknown owner.
Each risk must contain:
risk_id, category, description, likelihood, impact, evidence,
mitigation, affected_work_items, owner.

Allowed categories: technical, delivery, dependency, schedule.
Allowed likelihoods: low, medium, high.
Allowed impacts: low, medium, high, critical.
Use only the work-item IDs supplied below.
If evidence is insufficient, return fewer risks rather than inventing them.
Return JSON only, with no Markdown fences.

PROJECT SUMMARY:
{serialized}
"""



def _extract_work_items(
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prefer specification stories; fall back to candidate work items."""
    stories = context.get("stories")

    if isinstance(stories, list) and stories:
        return [
            _normalize_work_item(item, source="spec_story")
            for item in stories
        ]

    groups = context.get("groups")
    if isinstance(groups, list):
        grouped_stories = []
        for group in groups:
            if isinstance(group, dict):
                grouped_stories.extend(group.get("stories") or [])

        if grouped_stories:
            return [
                _normalize_work_item(item, source="spec_story")
                for item in grouped_stories
            ]

    candidates = context.get("candidate_work_items") or []
    return [
        _normalize_work_item(item, source="architecture_cwi")
        for item in candidates
    ]



def _normalize_work_item(
    item: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    """Normalize spec stories while preserving their available context."""

    architecture = item.get("architecture") or {}
    if not isinstance(architecture, dict):
        architecture = {}

    # The context assembler stores the blueprint under architecture.blueprint.
    blueprint = item.get("blueprint") or architecture.get("blueprint")

    # Spec stories use `content`; candidate work items may use `summary`.
    summary = (
        item.get("summary")
        or item.get("description")
        or item.get("content")
        or ""
    )

    metadata = item.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    work_item_id = (
        item.get("work_item_id")
        or item.get("story_id")
        or item.get("cwi_id")
        or item.get("artifact_id")
        or item.get("ref")
    )

    if not work_item_id:
        raise ValueError(
            f"Work item has no stable ID: {item.get('title', '<untitled>')}"
        )

    return {
        "work_item_id": str(work_item_id),
        "title": item.get("title") or str(work_item_id),
        "source_kind": source,
        "summary": summary,
        "acceptance_criteria": (
            item.get("acceptance_criteria")
            or item.get("acceptanceCriteria")
            or metadata.get("acceptance_criteria")
            or []
        ),
        "blueprint": blueprint,
        "architecture_baseline": None,
        "dependencies": item.get("dependencies") or metadata.get("dependencies") or [],
        "related": item.get("related") or [],
        "gaps": item.get("gaps") or metadata.get("gaps") or [],
        "conflicts": item.get("conflicts") or metadata.get("conflicts") or [],
        "decisions": item.get("decisions") or metadata.get("decisions") or [],
        "open_questions": (
            item.get("open_questions")
            or metadata.get("open_questions")
            or []
        ),
        "source_refs": (
            item.get("source_refs")
            or metadata.get("source_refs")
            or []
        ),
        "source_metadata": metadata,
    }



def _fingerprint(context: dict[str, Any]) -> str:
    serialized = json.dumps(
        context,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _deduplicate(values: list) -> list:
    """Deduplicate text while preserving the first occurrence."""
    import re
    import unicodedata

    result = []
    seen = set()

    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue

        normalized = unicodedata.normalize("NFKC", value)
        normalized = normalized.casefold().replace("\u2011", "-")
        normalized = re.sub(r"[\s\-]+", " ", normalized)
        normalized = re.sub(r"[?.!]+$", "", normalized).strip()

        # Normalize known equivalent saved-cards questions.
        normalized = normalized.replace(
            "is saved cards feature mvp or phase 2",
            "is the saved cards feature part of the mvp or planned for phase 2",
        )

        if normalized not in seen:
            seen.add(normalized)
            result.append(value.strip())

    return result


def _overall_confidence(estimates: list[dict[str, Any]]) -> str:
    levels = {"low": 0, "medium": 1, "high": 2}
    if not estimates:
        return "low"

    weakest = min(
        levels.get(item["confidence"], 0)
        for item in estimates
    )
    return {0: "low", 1: "medium", 2: "high"}[weakest]


def _summarize_risks(
    risks: list[dict[str, Any]],
) -> dict[str, Any]:
    categories = {
        "technical": 0,
        "delivery": 0,
        "dependency": 0,
        "schedule": 0,
    }
    impacts = {
        "low": 0,
        "medium": 0,
        "high": 0,
        "critical": 0,
    }

    for risk in risks:
        categories[risk["category"]] += 1
        impacts[risk["impact"]] += 1

    return {
        "total": len(risks),
        "by_category": categories,
        "by_impact": impacts,
        "high_or_critical_count": impacts["high"] + impacts["critical"],
    }


def run_estimation_risk(
    context_package: dict[str, Any],
    llm_json_call: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    agent = EstimationRiskAgent(llm_call=llm_json_call)
    return agent.run(context_package)

