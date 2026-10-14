
import argparse
import json
import logging
import time

from dotenv import load_dotenv

load_dotenv()

from agents.architecture.prompts import (
    ARCHITECTURE_SYSTEM_PROMPT,
    build_architecture_user_prompt,
)
from llm_client import call_llm_json
from substrate.context_assembler import build_architecture_package
from substrate.artifact_registry import upsert_artifact
from substrate.context_registry import record_provenance
from substrate.vector_store import store_chunks


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)

BLUEPRINT_TYPE = "architecture_blueprint"


def _decode_json(value):
    """Decode a JSON object from a dictionary or JSON string."""
    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            return {}

    return {}


def validate_blueprints(result: dict, package: dict) -> list:
    """Validate blueprint evidence against the supplied context package."""
    context = package["context"]
    cwis = context["candidate_work_items"]

    valid_evidence_ids = {
        str(artifact["artifact_id"])
        for artifact in context["artifacts"]
    }

    valid_evidence_ids.update(
        str(chunk["artifact_id"])
        for chunk in context["retrieved_chunks"]
        if chunk.get("artifact_id")
    )

    returned = {
        str(item.get("cwi_artifact_id")): item
        for item in result.get("blueprints", [])
        if isinstance(item, dict)
    }

    validated = []

    for cwi in cwis:
        cwi_artifact_id = str(cwi["artifact_id"])
        blueprint = returned.get(cwi_artifact_id, {})

        if not blueprint:
            logger.warning(
                "No blueprint returned for CWI %s; "
                "creating a review-required fallback.",
                cwi_artifact_id,
            )

        evidence = blueprint.get("evidence_artifact_ids", [])

        if not isinstance(evidence, list):
            evidence = []

        evidence = list(
            dict.fromkeys(
                str(item)
                for item in evidence
                if str(item) in valid_evidence_ids
            )
        )

        try:
            confidence = float(blueprint.get("confidence", 0.2))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.2

        metadata = _decode_json(cwi.get("metadata"))

        blueprint.update(
            {
                "cwi_artifact_id": cwi_artifact_id,
                "cwi_id": metadata.get("cwi_id"),
                "title": (
                    blueprint.get("title")
                    or cwi.get("title")
                    or "Architecture Blueprint"
                ),
                "problem_statement": (
                    blueprint.get("problem_statement")
                    or cwi.get("content")
                    or ""
                ),
                "confidence": confidence,
                "needs_review": (
                    bool(blueprint.get("needs_review", False))
                    or confidence < 0.70
                    or not evidence
                ),
                "evidence_artifact_ids": evidence,
            }
        )

        validated.append(blueprint)

    return validated


def persist_blueprint(
    blueprint: dict,
    cwi: dict,
    project_id: str | None,
) -> str:
    """Persist a blueprint, provenance, and vector chunks."""
    source_id = cwi.get("source_id")

    if not source_id:
        raise ValueError(
            f"CWI {cwi['artifact_id']} has no source_id. "
            "The artifact registry requires a source association."
        )

    metadata = {
        "artifact_kind": BLUEPRINT_TYPE,
        "cwi_artifact_id": blueprint["cwi_artifact_id"],
        "cwi_id": blueprint.get("cwi_id"),
        "current_state": blueprint.get("current_state", {}),
        "affected_components": blueprint.get("affected_components", []),
        "integration_points": blueprint.get("integration_points", []),
        "constraints": blueprint.get("constraints", []),
        "assumptions": blueprint.get("assumptions", []),
        "open_questions": blueprint.get("open_questions", []),
        "architecture_options": blueprint.get("architecture_options", []),
        "recommended_option": blueprint.get("recommended_option"),
        "architecture_blueprint": blueprint.get(
            "architecture_blueprint", {}
        ),
        "adr_candidates": blueprint.get("adr_candidates", []),
        "technical_risks": blueprint.get("technical_risks", []),
        "confidence": blueprint["confidence"],
        "needs_review": blueprint["needs_review"],
        "reasoning_summary": blueprint.get("reasoning_summary", ""),
        "evidence_artifact_ids": blueprint["evidence_artifact_ids"],
        "project_id": project_id,
    }

    stable_ref = (
        f"architecture-blueprint:{blueprint['cwi_artifact_id']}"
    )

    synthetic_artifact = {
        "title": blueprint["title"],
        "source_refs": [stable_ref],
    }

    artifact_id = upsert_artifact(
        cwi=synthetic_artifact,
        source_id=source_id,
        artifact_type=BLUEPRINT_TYPE,
        content_override=json.dumps(
            blueprint, ensure_ascii=False
        ),
        extra_metadata=metadata,
        project_id=project_id,
    )

    record_provenance(
        artifact_id=artifact_id,
        source_id=source_id,
        source_ref=cwi.get("source_ref") or cwi["artifact_id"],
        extraction_method="architecture_discovery_agent",
    )

    store_chunks(
        artifact_id,
        json.dumps(blueprint, ensure_ascii=False),
        project_id=project_id,
        metadata={
            "artifact_type": BLUEPRINT_TYPE,
            "cwi_artifact_id": cwi["artifact_id"],
        },
    )

    return artifact_id


def _build_single_cwi_package(package: dict, cwi: dict) -> dict:
    """Keep one CWI and its associated evidence in the prompt."""
    context = package["context"]
    cwi_metadata = _decode_json(cwi.get("metadata"))

    member_ids = {
        str(artifact_id)
        for artifact_id in cwi_metadata.get("member_artifact_ids", [])
    }

    source_artifacts = [
        artifact
        for artifact in context["artifacts"]
        if str(artifact.get("artifact_id")) in member_ids
    ]

    retrieved_chunks = [
        chunk
        for chunk in context["retrieved_chunks"]
        if str(chunk.get("artifact_id")) in member_ids
    ]

    return {
        **package,
        "context": {
            **context,
            "candidate_work_items": [cwi],
            "artifacts": source_artifacts,
            "retrieved_chunks": retrieved_chunks,
        },
    }


def generate_for_cwi(
    package: dict,
    cwi: dict,
) -> list:
    """Generate and validate architecture output for one CWI."""
    single_package = _build_single_cwi_package(package, cwi)

    result = call_llm_json(
        ARCHITECTURE_SYSTEM_PROMPT,
        build_architecture_user_prompt(single_package),
        max_tokens=4096,
    )

    return validate_blueprints(result, single_package)


def generate_with_retries(
    package: dict,
    cwi: dict,
    retries: int,
    retry_base_seconds: int,
) -> list:
    """
    Retry only this CWI after failures.

    retries=2 means at most 3 total attempts.
    Delays grow exponentially between attempts.
    """
    cwi_id = str(cwi["artifact_id"])
    title = cwi.get("title", cwi_id)

    for attempt in range(retries + 1):
        try:
            logger.info(
                "Generating blueprint for '%s' (attempt %d/%d)",
                title,
                attempt + 1,
                retries + 1,
            )

            blueprints = generate_for_cwi(package, cwi)

            if not blueprints:
                raise ValueError(
                    f"No blueprint output returned for CWI {cwi_id}"
                )

            return blueprints

        except Exception as exc:
            if attempt >= retries:
                logger.error(
                    "All generation attempts failed for CWI %s (%s): %s",
                    cwi_id,
                    title,
                    exc,
                    exc_info=True,
                )
                return []

            delay = retry_base_seconds * (2 ** attempt)

            logger.warning(
                "Generation attempt %d failed for CWI %s: %s. "
                "Retrying in %d seconds.",
                attempt + 1,
                cwi_id,
                exc,
                delay,
            )

            time.sleep(delay)

    return []


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run Architecture Discovery with targeted retries "
            "and accurate completion reporting."
        )
    )

    parser.add_argument(
        "--project-id",
        default=None,
        help="Project filter, e.g. KAN.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of CWIs to process.",
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Additional generation attempts per failed CWI. Default: 2.",
    )

    parser.add_argument(
        "--retry-base-seconds",
        type=int,
        default=15,
        help="Base delay for exponential backoff. Default: 15 seconds.",
    )

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")

    if args.retries < 0:
        parser.error("--retries cannot be negative")

    if args.retry_base_seconds < 1:
        parser.error("--retry-base-seconds must be at least 1")

    # Stage 1: Common Substrate -> architecture context package.
    package_path = build_architecture_package(
        project_id=args.project_id,
        limit=args.limit,
    )

    package = json.loads(
        package_path.read_text(encoding="utf-8")
    )

    context = package["context"]
    cwis = context["candidate_work_items"]

    logger.info(
        "Architecture context package built: %s",
        package_path,
    )

    logger.info(
        "Package contains %d CWIs, %d source artifacts, "
        "%d retrieved chunks",
        len(cwis),
        len(context["artifacts"]),
        len(context["retrieved_chunks"]),
    )

    if not cwis:
        logger.warning(
            "No candidate_work_item artifacts found. "
            "Run Discovery Agent first and verify project scoping."
        )
        return 0

    # Stage 2: Generate one blueprint per CWI.
    # Each failed CWI is retried independently.
    blueprints_by_cwi = {}
    generation_failures = {}

    for index, cwi in enumerate(cwis, start=1):
        cwi_id = str(cwi["artifact_id"])
        title = cwi.get("title", cwi_id)

        logger.info(
            "Processing architecture CWI %d/%d: %s",
            index,
            len(cwis),
            title,
        )

        generated = generate_with_retries(
            package=package,
            cwi=cwi,
            retries=args.retries,
            retry_base_seconds=args.retry_base_seconds,
        )

        if generated:
            blueprints_by_cwi[cwi_id] = generated
            generation_failures.pop(cwi_id, None)
        else:
            generation_failures[cwi_id] = title

    # Stage 3: Retry ONLY failed CWIs.
    # Successful generations are not sent to the LLM again.
    if generation_failures:
        logger.warning(
            "%d CWI(s) still failed generation. "
            "Starting a targeted recovery pass.",
            len(generation_failures),
        )

    recovery_round = 0
    max_recovery_rounds = 2

    while generation_failures and recovery_round < max_recovery_rounds:
        recovery_round += 1
        delay = args.retry_base_seconds * (2 ** (recovery_round - 1))

        logger.info(
            "Recovery round %d/%d: retrying %d failed CWI(s) "
            "after %d seconds.",
            recovery_round,
            max_recovery_rounds,
            len(generation_failures),
            delay,
        )

        time.sleep(delay)

        failed_ids = list(generation_failures.keys())

        for cwi_id in failed_ids:
            cwi = next(
                (
                    item for item in cwis
                    if str(item["artifact_id"]) == cwi_id
                ),
                None,
            )

            if cwi is None:
                logger.error(
                    "Failed CWI %s is no longer in the context package.",
                    cwi_id,
                )
                continue

            title = cwi.get("title", cwi_id)

            logger.info(
                "Recovery round %d: retrying CWI '%s'",
                recovery_round,
                title,
            )

            # One attempt in each recovery round. The SDK may also
            # perform its own internal HTTP retries.
            try:
                generated = generate_for_cwi(package, cwi)

                if generated:
                    blueprints_by_cwi[cwi_id] = generated
                    generation_failures.pop(cwi_id, None)

                    logger.info(
                        "Recovery succeeded for CWI '%s'",
                        title,
                    )
                else:
                    logger.warning(
                        "Recovery returned no blueprint for CWI '%s'",
                        title,
                    )

            except Exception as exc:
                logger.error(
                    "Recovery round %d failed for CWI %s (%s): %s",
                    recovery_round,
                    cwi_id,
                    title,
                    exc,
                    exc_info=True,
                )

    # Stage 4: Persist generated blueprints.
    cwi_by_id = {
        str(cwi["artifact_id"]): cwi
        for cwi in cwis
    }

    persisted_cwi_ids = set()
    persistence_failures = {}

    total_generated = sum(
        len(items) for items in blueprints_by_cwi.values()
    )

    logger.info(
        "Architecture reasoning produced %d blueprint(s) "
        "for %d/%d CWIs",
        total_generated,
        len(blueprints_by_cwi),
        len(cwis),
    )

    for cwi_id, generated_blueprints in blueprints_by_cwi.items():
        cwi = cwi_by_id.get(cwi_id)

        if cwi is None:
            logger.error(
                "Cannot persist blueprint: CWI %s was not found",
                cwi_id,
            )
            persistence_failures[cwi_id] = "CWI not found"
            continue

        cwi_metadata = _decode_json(cwi.get("metadata"))

        project_id = (
            args.project_id
            or cwi_metadata.get("project_id")
        )

        all_persisted = True

        for blueprint in generated_blueprints:
            try:
                artifact_id = persist_blueprint(
                    blueprint,
                    cwi,
                    project_id,
                )

                logger.info(
                    "Created/updated blueprint '%s' "
                    "(artifact_id=%s, confidence=%.2f, needs_review=%s)",
                    blueprint["title"],
                    artifact_id,
                    blueprint["confidence"],
                    blueprint["needs_review"],
                )

            except Exception as exc:
                all_persisted = False

                logger.error(
                    "Failed to persist blueprint for CWI %s: %s",
                    cwi_id,
                    exc,
                    exc_info=True,
                )

        if all_persisted:
            persisted_cwi_ids.add(cwi_id)
            persistence_failures.pop(cwi_id, None)
        else:
            persistence_failures[cwi_id] = (
                "One or more blueprint persistence operations failed"
            )

    # Stage 5: Final accounting against ALL input CWIs.
    unresolved = {}

    for cwi_id, title in generation_failures.items():
        unresolved[cwi_id] = f"{title}: generation failed"

    for cwi_id, reason in persistence_failures.items():
        unresolved[cwi_id] = (
            f"{cwi_by_id.get(cwi_id, {}).get('title', cwi_id)}: {reason}"
        )

    successful_cwis = len(persisted_cwi_ids)
    failed_cwis = len(cwis) - successful_cwis

    logger.info("=" * 72)
    logger.info("ARCHITECTURE DISCOVERY FINAL SUMMARY")
    logger.info("=" * 72)
    logger.info("Input CWIs: %d", len(cwis))
    logger.info("CWIs with generated blueprints: %d", len(blueprints_by_cwi))
    logger.info("Blueprints generated: %d", total_generated)
    logger.info("CWIs with successfully stored blueprints: %d", successful_cwis)
    logger.info("CWIs unresolved: %d", failed_cwis)
    logger.info("Generation failures remaining: %d", len(generation_failures))
    logger.info("Persistence failures: %d", len(persistence_failures))

    if unresolved:
        logger.warning("Unresolved CWIs:")
        for cwi_id, reason in unresolved.items():
            logger.warning("  CWI %s — %s", cwi_id, reason)

    if failed_cwis == 0:
        logger.info(
            "Architecture Discovery complete: %d/%d CWIs successfully stored",
            successful_cwis,
            len(cwis),
        )
        return 0

    logger.error(
        "Architecture Discovery incomplete: %d/%d CWIs successfully stored; "
        "%d remain unresolved.",
        successful_cwis,
        len(cwis),
        failed_cwis,
    )

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
