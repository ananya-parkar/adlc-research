
"""
Product Owner Human Review Gate.

Fetches the active proposed priority backlog for a project,
collects a PO decision, and persists the review in artifact metadata.
"""

import json
from datetime import datetime, timezone

from substrate.artifact_registry import get_artifacts_by_type
from substrate.db import get_connection


PRIORITY_TYPE = "priority_backlog"

DECISIONS = {
    "1": "approved",
    "2": "rejected",
    "3": "changes_requested",
}


def get_pending_backlog(project_id: str):
    """Return the latest active, proposed backlog for this project."""
    artifacts = get_artifacts_by_type(PRIORITY_TYPE)

    candidates = []

    for artifact in artifacts:
        metadata = artifact.get("metadata") or {}

        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        if metadata.get("project_id") != project_id:
            continue

        if metadata.get("review_status", "proposed") != "proposed":
            continue

        if metadata.get("lifecycle", "active") == "superseded":
            continue

        artifact["metadata"] = metadata
        candidates.append(artifact)

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item.get("updated_at") or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True,
    )

    return candidates[0]


def display_backlog(artifact: dict):
    metadata = artifact["metadata"]

    print("\n" + "=" * 90)
    print("PRODUCT OWNER — PRIORITY BACKLOG REVIEW")
    print("=" * 90)

    print(f"\nProject       : {metadata.get('project_id')}")
    print(f"Backlog       : {artifact.get('title')}")
    print(f"Artifact ID   : {artifact.get('artifact_id')}")
    print(f"Policy        : {metadata.get('policy_version', 'N/A')}")
    print(f"Created       : {metadata.get('created_at', 'N/A')}")
    print(f"Review status : {metadata.get('review_status', 'proposed')}")

    ranked = metadata.get("ranked_items") or []
    unranked = metadata.get("unranked_or_blocked_items") or []
    questions = metadata.get("open_questions") or []

    print("\n" + "-" * 90)
    print(f"RANKED BACKLOG ({len(ranked)} items)")
    print("-" * 90)

    if not ranked:
        print("No ranked items.")

    for item in ranked:
        factor = item.get("factor_breakdown") or {}
        job_size = factor.get("job_size") or {}

        print(
            f"\n{item.get('rank', '-'):>3}. "
            f"[{item.get('priority_label', 'N/A')}] "
            f"{item.get('key') or 'NEW'} — {item.get('title', 'Untitled')}"
        )
        print(f"     Score       : {item.get('score', 'N/A')}")
        print(f"     Effort      : {job_size.get('expected_hours', 'N/A')} hours")
        print(f"     Rationale   : {item.get('rationale', 'N/A')}")
        print(f"     Needs review: {item.get('needs_review', False)}")

        for reason in item.get("review_reasons") or []:
            print(f"     Review flag : {reason}")

    print("\n" + "-" * 90)
    print(f"UNRANKED / BLOCKED ITEMS ({len(unranked)} items)")
    print("-" * 90)

    if not unranked:
        print("No unranked or blocked items.")

    for item in unranked:
        print(
            f"- {item.get('key') or 'NEW'} — "
            f"{item.get('title', 'Untitled')}"
        )
        print(f"  Reason: {item.get('reason', 'Not specified')}")

    print("\n" + "-" * 90)
    print(f"OPEN QUESTIONS ({len(questions)})")
    print("-" * 90)

    if not questions:
        print("No open questions.")

    for index, question in enumerate(questions, start=1):
        print(f"{index}. {question}")

    print("\n" + "=" * 90)


def collect_review():
    """Collect a valid decision and reviewer details."""
    print("\nPO REVIEW DECISION")
    print("1. Approve backlog")
    print("2. Reject backlog")
    print("3. Request changes")

    while True:
        choice = input("\nSelect decision (1/2/3): ").strip()

        if choice in DECISIONS:
            decision = DECISIONS[choice]
            break

        print("Invalid choice. Enter 1, 2, or 3.")

    while True:
        reviewer = input("Reviewer name: ").strip()

        if reviewer:
            break

        print("Reviewer name is required.")

    comments = input("Comments (optional): ").strip()

    if decision == "rejected" and not comments:
        print("Please provide a reason for rejecting the backlog.")
        while not comments:
            comments = input("Rejection reason: ").strip()

    if decision == "changes_requested" and not comments:
        print("Please describe the changes required.")
        while not comments:
            comments = input("Required changes: ").strip()

    return {
        "decision": decision,
        "reviewer": reviewer,
        "comments": comments,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }


def save_review(artifact_id: str, review: dict) -> bool:
    """
    Save the decision only if the backlog is still proposed.
    The conditional UPDATE prevents duplicate or concurrent reviews.
    """
    status = review["decision"]

    update_metadata = {
        "review_status": status,
        "needs_review": status == "changes_requested",
        "review": review,
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE artifacts
                   SET metadata =
                       metadata
                       || %s::jsonb
                       || jsonb_build_object(
                           'review_history',
                           COALESCE(
                               metadata->'review_history',
                               '[]'::jsonb
                           ) || jsonb_build_array(%s::jsonb)
                       ),
                       updated_at = NOW(),
                       version = version + 1
                 WHERE artifact_id = %s
                   AND artifact_type = %s
                   AND COALESCE(
                       metadata->>'review_status',
                       'proposed'
                   ) = 'proposed'
                   AND COALESCE(
                       metadata->>'lifecycle',
                       'active'
                   ) <> 'superseded'
                RETURNING artifact_id
                """,
                (
                    json.dumps(update_metadata),
                    json.dumps(review),
                    artifact_id,
                    PRIORITY_TYPE,
                ),
            )

            updated = cur.fetchone()

        conn.commit()

    return updated is not None


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Product Owner Human Review Gate"
    )
    parser.add_argument(
        "--project-id",
        required=True,
        help="Project whose proposed priority backlog should be reviewed.",
    )
    args = parser.parse_args()

    print("\nFetching proposed priority backlog...")

    artifact = get_pending_backlog(args.project_id)

    if artifact is None:
        print(
            f"\nNo pending proposed priority backlog found "
            f"for project {args.project_id}."
        )
        return 1

    display_backlog(artifact)

    review = collect_review()

    print("\n" + "-" * 60)
    print("REVIEW CONFIRMATION")
    print("-" * 60)
    print(f"Project  : {args.project_id}")
    print(f"Decision : {review['decision'].upper()}")
    print(f"Reviewer : {review['reviewer']}")
    print(f"Comments : {review['comments'] or 'None'}")
    print("-" * 60)

    while True:
        confirmation = input("Save this review? (y/n): ").strip().lower()

        if confirmation in ("y", "yes"):
            break

        if confirmation in ("n", "no"):
            print("Review cancelled. No changes were saved.")
            return 0

        print("Enter y or n.")

    try:
        saved = save_review(
            str(artifact["artifact_id"]),
            review,
        )
    except Exception as exc:
        print(f"\nERROR: Could not save review: {exc}")
        return 2

    if not saved:
        print(
            "\nERROR: This backlog is no longer proposed or active. "
            "It may already have been reviewed or superseded. "
            "No review was saved."
        )
        return 2

    print("\n" + "=" * 60)
    print("PO REVIEW SAVED SUCCESSFULLY")
    print("=" * 60)
    print(f"Decision : {review['decision']}")
    print(f"Reviewer : {review['reviewer']}")
    print(f"Time     : {review['reviewed_at']}")
    print(f"Artifact : {artifact['artifact_id']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())