# agents/discovery/discovery_agent.py
import hashlib
import json
import logging
from typing import Dict, List

from dotenv import load_dotenv
load_dotenv()

from agents.discovery.prompts import (
    DISCOVERY_SYNTHESIS_SYSTEM_PROMPT,
    build_synthesis_user_prompt,
)
from llm_client import call_llm_json
from substrate.artifact_registry import upsert_artifact
from substrate.context_assembler import build_discovery_package
from substrate.context_registry import record_provenance
from substrate.vector_store import store_chunks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)

RAW_TYPE = "source_artifact"
CWI_TYPE = "candidate_work_item"


def _cluster_ref(member_ids: List[str]) -> str:
    joined = ",".join(sorted(member_ids))
    return "cluster:" + hashlib.sha256(
        joined.encode()
    ).hexdigest()[:16]


def _validate_work_items(
    work_items: list,
    artifacts_by_id: Dict[str, dict],
) -> list:

    valid_ids = set(artifacts_by_id)

    validated = []
    assigned = set()

    for item in work_items:

        member_ids = item.get("member_ids", [])

        member_ids = [
            artifact_id
            for artifact_id in member_ids
            if artifact_id in valid_ids
        ]

        if not member_ids:
            logger.warning(
                "Skipping work item with no valid members: %s",
                item.get("title"),
            )
            continue

        # Prevent the same source artifact from appearing
        # in multiple work items.
        duplicate_members = assigned.intersection(member_ids)

        if duplicate_members:
            logger.warning(
                "Removing already-assigned artifacts from '%s': %s",
                item.get("title"),
                duplicate_members,
            )

            member_ids = [
                artifact_id
                for artifact_id in member_ids
                if artifact_id not in assigned
            ]

        if not member_ids:
            continue

        assigned.update(member_ids)

        item["member_ids"] = member_ids
        validated.append(item)

    # Never silently lose source artifacts.
    missing = valid_ids - assigned

    for artifact_id in missing:
        artifact = artifacts_by_id[artifact_id]

        validated.append(
            {
                "member_ids": [artifact_id],
                "title": artifact.get("title") or "Unclassified discovery item",
                "business_intent": "",
                "refined_summary": artifact.get("content") or "",
                "signal_type": (
                    artifact.get("metadata") or {}
                ).get("signal_type", "unclear"),
                "is_actionable": False,
                "needs_review": True,
                "confidence": 0.2,
                "reasoning": "Artifact was not assigned by the LLM and requires review.",
                "evidence": [],
            }
        )

    return validated


def process_work_item(
    work_item: dict,
    artifacts_by_id: Dict[str, dict],
) -> None:

    member_ids = work_item["member_ids"]

    members = [
        artifacts_by_id[artifact_id]
        for artifact_id in member_ids
    ]

    primary = members[0]

    primary_source = primary.get("source") or {}
    primary_project_id = (
        primary.get("metadata") or {}
    ).get("project_id")

    synthetic_cwi = {
        "title": work_item.get("title"),
        "source_refs": [
            _cluster_ref(member_ids)
        ],
    }

    needs_review = (
        work_item.get("needs_review", False)
        or not work_item.get("is_actionable", True)
        or work_item.get("confidence", 0.0) < 0.70
        or work_item.get("signal_type") == "unclear"
    )

    cwi_id = f"cwi-{_cluster_ref(member_ids).replace('cluster:', '')}"
    cwi_artifact_id = upsert_artifact(
        cwi=synthetic_cwi,
        source_id=primary_source.get("source_id"),
        artifact_type=CWI_TYPE,
        content_override=work_item.get("refined_summary"),
        extra_metadata={
            "cwi_id": cwi_id,
            "business_intent": work_item.get("business_intent"),
            "signal_type": work_item.get("signal_type"),
            "is_actionable": work_item.get("is_actionable"),
            "needs_review": needs_review,
            "confidence": work_item.get("confidence"),
            "reasoning": work_item.get("reasoning"),
            "member_artifact_ids": member_ids,
            "member_count": len(member_ids),
            "evidence": [
                {
                    "artifact_id": member["artifact_id"],
                    "source_type": (member.get("metadata") or {}).get("source_type"),
                    "source_ref": (member.get("source") or {}).get("source_ref"),
                }
                for member in members
            ],
        },
        project_id=primary_project_id,
    )

    for member in members:

        source = member.get("source") or {}

        record_provenance(
            artifact_id=cwi_artifact_id,
            source_id=source.get("source_id"),
            source_ref=source.get("source_ref"),
            extraction_method=(
                "discovery_agent_llm_merge"
                if len(members) > 1
                else "discovery_agent_llm"
            ),
        )

    store_chunks(
        cwi_artifact_id,
        work_item.get("refined_summary")
        or work_item.get("title")
        or "",
        project_id=primary_project_id,
        metadata={
            "member_count": len(member_ids),
            "artifact_type": CWI_TYPE,
        },
    )

    logger.info(
        "Created/updated CWI '%s' from %d artifact(s)",
        work_item.get("title"),
        len(member_ids),
    )


def main() -> None:

    package_path = build_discovery_package()

    logger.info(
        "Package built at %s",
        package_path,
    )

    package = json.loads(
        package_path.read_text(
            encoding="utf-8"
        )
    )

    all_artifacts = package["context"]["artifacts"]

    raw_artifacts = [
        artifact
        for artifact in all_artifacts
        if artifact.get("artifact_type") == RAW_TYPE
    ]

    logger.info(
        "Package contains %d total artifacts; %d source artifacts for discovery",
        len(all_artifacts),
        len(raw_artifacts),
    )

    if not raw_artifacts:
        logger.warning(
            "No source_artifact records found. "
            "Run the source ingestion again before running Discovery Agent."
        )
        return

    artifacts_by_id = {
        artifact["artifact_id"]: artifact
        for artifact in raw_artifacts
    }

    user_prompt = build_synthesis_user_prompt(
        raw_artifacts
    )

    result = call_llm_json(
        DISCOVERY_SYNTHESIS_SYSTEM_PROMPT,
        user_prompt,
        max_tokens=8192,
    )

    work_items = result.get("work_items", [])
    patterns = result.get("patterns", [])

    work_items = _validate_work_items(
        work_items,
        artifacts_by_id,
    )

    logger.info(
        "Discovery Agent produced %d candidate work items and %d patterns",
        len(work_items),
        len(patterns),
    )

    succeeded = 0

    for work_item in work_items:

        try:
            process_work_item(
                work_item,
                artifacts_by_id,
            )
            succeeded += 1

        except Exception as exc:

            logger.exception(
                "Failed to store CWI '%s': %s",
                work_item.get("title"),
                exc,
            )

    logger.info(
        "Discovery complete: %d CWIs stored",
        succeeded,
    )

    for pattern in patterns:

        logger.info(
            "DISCOVERY PATTERN: %s",
            pattern.get("pattern"),
        )


if __name__ == "__main__":
    main()