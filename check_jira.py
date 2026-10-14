# check_jira.py
"""
Dry-run check for the new Jira connector + mapper.
Fetches a few issues and prints what would be stored. Writes NOTHING to the DB.

Usage:
    python check_jira.py            # first 3 issues
    python check_jira.py --limit 5
    python check_jira.py --key KAN-12   # dump one issue fully
"""

import argparse
import json
import os

from dotenv import load_dotenv

load_dotenv()

from auth import ApiTokenAuth  # noqa: E402
from connectors.jira_connector import JiraConnector  # noqa: E402
from mappers.jira_mapper import map_issue_to_cwi  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--key", help="Only show this issue key")
    args = p.parse_args()

    connector = JiraConnector(
        base_url=os.environ["JIRA_URL"],
        auth=ApiTokenAuth(email=os.environ["JIRA_EMAIL"], api_token=os.environ["JIRA_API_TOKEN"]),
        project_key=os.environ.get("JIRA_PROJECT_KEY", "PROJ"),
    )

    if not connector.test_connection():
        raise SystemExit("✗ Connection failed — check .env")

    print("\n=== Custom fields resolved on this Jira ===")
    print(json.dumps(connector.resolve_custom_fields(), indent=2))

    shown = 0
    for issue in connector.fetch_items():
        if args.key and issue["key"] != args.key:
            continue

        cwi = map_issue_to_cwi(issue)
        m = cwi["metadata"]

        print("\n" + "=" * 80)
        print(f"{issue['key']}  |  signal_type={cwi['signal_type']}  |  needs_review={cwi['needs_review']}")
        print("=" * 80)
        print(cwi["raw_summary"])
        print("\n--- key metadata ---")
        for k in ("issue_type", "priority", "labels", "components", "epic_key", "story_points",
                  "current_sprint", "assignee", "comment_count", "quality_flags"):
            print(f"  {k:<16}: {m.get(k)}")
        print(f"  {'acceptance':<16}: {'YES' if m.get('acceptance_criteria') else 'none'}")
        print(f"  {'links':<16}: {len(m['links'])}   status_changes: {len(m['history']['status_changes'])}")
        print(f"  {'relations':<16}: {cwi['relations']}")

        if args.key:
            print("\n--- full metadata JSON ---")
            print(json.dumps(m, indent=2, default=str))

        shown += 1
        if shown >= (1 if args.key else args.limit):
            break

    if shown == 0:
        print("\n✗ No issues returned. Check JIRA_PROJECT_KEY or the --key value.")
    else:
        print(f"\n✓ Mapped {shown} issue(s). Nothing was written to the database.")


if __name__ == "__main__":
    main()