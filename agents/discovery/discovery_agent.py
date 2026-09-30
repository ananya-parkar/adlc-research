"""
Discovery Agent — batch synthesis, not 1:1 mapping.

Instead of refining each raw item independently, this reads ALL raw
items from the package together and asks the LLM to decide how many
distinct CWIs actually exist — merging items that describe the same
underlying work (e.g. a Jira ticket + a requirements doc about the
same feature) into one CWI, rather than producing one CWI per item.

A merged CWI is linked to ALL its source items via multiple
provenance records (the artifacts table itself only needs one
source_id/source_ref pair for its own conflict key — a synthetic
"cluster:<hash>" ref is used there — but artifact_provenance holds
one row per contributing raw item, so nothing about "what fed into
this CWI" is lost).

Usage:
    python -m agents.discovery.discovery_agent
"""

import hashlib
import json
import logging

from dotenv import load_dotenv

load_dotenv()

from agents.discovery.prompts import (  # noqa: E402
    DISCOVERY_SYNTHESIS_SYSTEM_PROMPT,
    build_synthesis_user_prompt,
)
from llm_client import call_llm_json  # noqa: E402
from substrate.artifact_registry import upsert_artifact  # noqa: E402
from substrate.context_assembler import build_discovery_package  # noqa: E402
from substrate.context_registry import record_provenance  # noqa: E402
from substrate.vector_store import store_chunks  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

RAW_TYPE = "source_artifact"
CWI_TYPE = "candidate_work_item"


def _cluster_ref(member_ids: list) -> str:
    """Stable synthetic source_ref for a merged CWI's own artifacts-table row.
    Same set of members always produces the same ref, so re-running is idempotent."""
    joined = ",".join(sorted(member_ids))
    return "cluster:" + hashlib.sha256(joined.encode()).hexdigest()[:16]


def process_work_item(work_item: dict, artifacts_by_id: dict) -> None:
    member_ids = work_item.get("member_ids", [])
    members = [artifacts_by_id[mid] for mid in member_ids if mid in artifacts_by_id]
    if not members:
        logger.warning("Work item '%s' has no resolvable members, skipping", work_item.get("title"))
        return

    # The artifacts table needs ONE source_id/source_ref for its own
    # conflict key — use the first member's source, and a synthetic
    # ref representing the whole cluster.
    primary_source = members[0].get("source") or {}
    # project_id is read from the member's own metadata (denormalized
    # there by store_cwis.py/store_file.py) — assumes all members of
    # one cluster belong to the same project, which should hold since
    # Discovery Agent only ever processes one project's package at a time.
    primary_project_id = (members[0].get("metadata") or {}).get("project_id")
    synthetic_cwi = {
        "title": work_item.get("title"),
        "source_refs": [_cluster_ref(member_ids)],
    }

    cwi_artifact_id = upsert_artifact(
        cwi=synthetic_cwi,
        source_id=primary_source.get("source_id"),
        artifact_type=CWI_TYPE,
        content_override=work_item.get("refined_summary"),
        extra_metadata={
            "signal_type": work_item.get("signal_type"),
            "is_actionable": work_item.get("is_actionable"),
            "confidence": work_item.get("confidence"),
            "reasoning": work_item.get("reasoning"),
            "member_artifact_ids": member_ids,
            "member_count": len(members),
        },
        project_id=primary_project_id,
    )

    # Link this ONE CWI back to EVERY raw item that contributed to it —
    # this is what actually represents the merge, not the artifacts
    # table row itself.
    for member in members:
        member_source = member.get("source") or {}
        record_provenance(
            artifact_id=cwi_artifact_id,
            source_id=member_source.get("source_id"),
            source_ref=member_source.get("source_ref"),
            extraction_method="discovery_agent_llm_merge" if len(members) > 1 else "discovery_agent_llm",
        )

    # Embed the synthesized CWI text so it's semantically searchable —
    # this is what lets Spec Synthesizer (or future dedup passes) find
    # related/similar CWIs instead of only exact keyword matches.
    store_chunks(
        cwi_artifact_id,
        work_item.get("refined_summary") or work_item.get("title") or "",
        project_id=primary_project_id,
        metadata={"member_count": len(members)},
    )

    logger.info(
        "CWI '%s' ← %d member(s) [%s] actionable=%s confidence=%s",
        work_item.get("title"), len(members),
        ", ".join(m.get("source", {}).get("source_ref", "?") for m in members),
        work_item.get("is_actionable"), work_item.get("confidence"),
    )


def main() -> None:
    package_path = build_discovery_package()
    logger.info("Package built at %s", package_path)

    package = json.loads(package_path.read_text())
    all_artifacts = package["context"]["artifacts"]
    raw_artifacts = [a for a in all_artifacts if a["artifact_type"] == RAW_TYPE]
    logger.info("Package contains %d artifacts total, %d raw items to synthesize",
                len(all_artifacts), len(raw_artifacts))

    if not raw_artifacts:
        logger.info("No raw items to process.")
        return

    artifacts_by_id = {a["artifact_id"]: a for a in raw_artifacts}

    user_prompt = build_synthesis_user_prompt(raw_artifacts)
    llm_result = call_llm_json(
        DISCOVERY_SYNTHESIS_SYSTEM_PROMPT, user_prompt, max_tokens=4096,
    )

    work_items = llm_result.get("work_items", [])
    logger.info("LLM synthesized %d raw items into %d distinct CWIs", len(raw_artifacts), len(work_items))

    succeeded = 0
    failed = 0
    for work_item in work_items:
        try:
            process_work_item(work_item, artifacts_by_id)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to process work item '%s': %s", work_item.get("title"), exc)
            failed += 1

    logger.info("Done. %d CWIs created/updated, %d failed.", succeeded, failed)


if __name__ == "__main__":
    main()