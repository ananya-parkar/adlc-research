# run_estimation_risk.py

"""CLI runner for the Estimation & Risk Agent."""

import argparse
import json
from pathlib import Path

from agents.estimation_risk.estimation_risk_agent import EstimationRiskAgent
from substrate.context_assembler import build_estimation_risk_package
from substrate.artifact_registry import upsert_artifact
from substrate.context_registry import register_source, record_provenance
from substrate.vector_store import store_chunks

def persist_estimation_result(
    result: dict,
    package: dict,
) -> str:
    """Persist a project-level estimation result and its provenance."""

    context = package.get("context", package)
    project_id = result.get("project_id") or context.get("project_id")

    if not project_id:
        raise ValueError("Cannot persist an estimation result without project_id.")

    # Register a stable source so reruns update the same project artifact.
    source_id = register_source(
        source_type="agent_output",
        source_name="Estimation & Risk Agent",
        source_config={"agent": "estimation_risk"},
    )

    source_ref = f"estimation-risk:{project_id}"
    content = json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        default=str,
    )

    artifact_id = upsert_artifact(
        cwi={
            "title": f"Estimation & Risk Assessment — {project_id}",
            "source_refs": [source_ref],
            "project_id": project_id,
        },
        source_id=source_id,
        artifact_type="estimation_risk",
        content_override=content,
        extra_metadata={
            "project_id": project_id,
            "lifecycle": "active",
            "source_fingerprint": result.get("source_fingerprint"),
            "schema_version": result.get("schema_version"),
            "estimate_unit": result.get("estimate_unit"),
            "estimation_method": result.get("estimation_method"),
            "project_effort": result.get("project_effort"),
            "risk_summary": result.get("risk_summary"),
            "overall_confidence": result.get("overall_confidence"),
            "needs_review": result.get("needs_review", False),
            "story_count": len(result.get("work_item_estimates", [])),
        },
        project_id=project_id,
    )

    # Link the estimation artifact to every story included in the package.
    stories = context.get("stories") or []
    seen = set()

    for story in stories:
        story_artifact_id = story.get("artifact_id") or story.get("work_item_id")
        story_source_id = story.get("source_id")

        if not story_artifact_id or not story_source_id:
            continue

        provenance_key = (str(story_source_id), str(story_artifact_id))
        if provenance_key in seen:
            continue
        seen.add(provenance_key)

        record_provenance(
            artifact_id=artifact_id,
            source_id=str(story_source_id),
            source_ref=str(story_artifact_id),
            extraction_method="estimation_risk_agent",
        )

    # Re-index the canonical artifact content for semantic retrieval.
    store_chunks(
        artifact_id,
        content,
        project_id=project_id,
        metadata={
            "artifact_type": "estimation_risk",
            "project_id": project_id,
            "source_fingerprint": result.get("source_fingerprint"),
        },
    )

    return artifact_id

def main():
    parser = argparse.ArgumentParser(
        description="Run the ADLC Estimation & Risk Agent."
    )
    parser.add_argument(
        "--project-id",
        required=True,
        help="Project identifier, e.g. KAN",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of stories to estimate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the package without calling the LLM.",
    )
    args = parser.parse_args()

    package_path = build_estimation_risk_package(
        project_id=args.project_id,
        limit=args.limit,
    )
    package = json.loads(
        Path(package_path).read_text(encoding="utf-8")
    )

    print(f"Package: {package_path}")
    print(f"Stories found: {package['context']['counts']['stories']}")

    if args.dry_run:
        print("Dry run complete. LLM was not called.")
        return

    agent = EstimationRiskAgent()
    result = agent.run(package)

    artifact_id = persist_estimation_result(result, package)
    print(f"Persisted estimation artifact: {artifact_id}")

    output_path = (
        Path("storage/packages")
        / f"estimation_risk_result_{args.project_id}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    print(f"Estimation result: {output_path}")
    print(f"Total expected effort: {result['project_effort']['expected_hours']} person-hours")
    print(f"Stories requiring review: {len(result['work_items_requiring_review'])}")
    print(f"Risks identified: {result['risk_summary']['total']}")


if __name__ == "__main__":
    main()
