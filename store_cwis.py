"""
Run this to sync issues from Jira into local CWI storage.

Usage:
    cp .env.example .env      # fill in your Jira details
    pip install -r requirements.txt
    python store_cwis.py              # incremental sync (only changed issues)
    python store_cwis.py --full       # full sync (ignores last-sync timestamp)
"""

import argparse
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from auth import ApiTokenAuth  # noqa: E402
from cwi_mapper import map_issue_to_cwi  # noqa: E402
from jira_connector import JiraConnector  # noqa: E402
from sync_state import get_last_sync, set_last_sync  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_PATH = Path(__file__).parent / "cwis.json"


def load_existing() -> dict:
    if OUTPUT_PATH.exists():
        return json.loads(OUTPUT_PATH.read_text())
    return {}


def save(cwis_by_id: dict) -> None:
    OUTPUT_PATH.write_text(json.dumps(cwis_by_id, indent=2, default=str))


def build_connector() -> JiraConnector:
    auth = ApiTokenAuth(
        email=os.environ["JIRA_EMAIL"],
        api_token=os.environ["JIRA_API_TOKEN"],
    )
    return JiraConnector(
        base_url=os.environ["JIRA_URL"],
        auth=auth,
        project_key=os.environ.get("JIRA_PROJECT_KEY", "PROJ"),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Ignore last-sync state, fetch everything")
    args = parser.parse_args()

    connector = build_connector()

    if not connector.test_connection():
        logger.error("Connection test failed — check your .env credentials")
        return

    sync_key = f"jira:{connector.project_key}"
    since = None if args.full else get_last_sync(sync_key)
    logger.info("Syncing since: %s", since or "(full sync)")

    cwis_by_id = load_existing()
    new_count = 0
    updated_count = 0

    for issue in connector.fetch_items(since=since):
        cwi = map_issue_to_cwi(issue)
        cwi_id = cwi["cwi_id"]
        if cwi_id in cwis_by_id:
            updated_count += 1
        else:
            new_count += 1
        cwis_by_id[cwi_id] = cwi

    save(cwis_by_id)
    set_last_sync(sync_key)

    logger.info(
        "Done. %d new CWIs, %d updated. Total: %d. Saved to %s",
        new_count, updated_count, len(cwis_by_id), OUTPUT_PATH,
    )


if __name__ == "__main__":
    main()