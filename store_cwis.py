"""
Run this to pull issues from a Jira project and store them as
Candidate Work Items (CWIs) in a local JSON file.

Usage:
    cp .env.example .env      # fill in your Jira details
    pip install -r requirements.txt
    python store_cwis.py

Swapping storage later: everything below `load_existing()` /
`save()` is the only part that needs to change to move to Postgres —
the fetch + mapping logic stays identical.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before importing jira_client so env vars are set

from jira_client import fetch_issues  # noqa: E402
from cwi_mapper import map_issue_to_cwi  # noqa: E402

OUTPUT_PATH = Path(__file__).parent / "cwis.json"


def load_existing() -> dict:
    if OUTPUT_PATH.exists():
        return json.loads(OUTPUT_PATH.read_text())
    return {}


def save(cwis_by_id: dict) -> None:
    OUTPUT_PATH.write_text(json.dumps(cwis_by_id, indent=2, default=str))


def main() -> None:
    project_key = os.environ.get("JIRA_PROJECT_KEY", "PROJ")
    jql = f"project = {project_key} ORDER BY updated DESC"

    cwis_by_id = load_existing()
    new_count = 0
    updated_count = 0

    for issue in fetch_issues(jql):
        cwi = map_issue_to_cwi(issue)
        cwi_id = cwi["cwi_id"]
        if cwi_id in cwis_by_id:
            updated_count += 1
        else:
            new_count += 1
        cwis_by_id[cwi_id] = cwi

    save(cwis_by_id)
    print(f"Done. {new_count} new CWIs, {updated_count} updated. Total: {len(cwis_by_id)}")
    print(f"Saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()