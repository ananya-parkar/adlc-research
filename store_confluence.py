"""
Run this to sync pages from a Confluence space into local CWI storage.
Adds to the same cwis.json used by store_cwis.py (Jira) — CWIs from
different sources coexist in one pool, distinguished by their
cwi_id prefix (cwi-jira-*, cwi-confluence-*).

Usage:
    python store_confluence.py              # incremental sync
    python store_confluence.py --full        # full sync
"""

import argparse
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from auth import ApiTokenAuth  # noqa: E402
from connectors.confluence_connector import ConfluenceConnector  # noqa: E402
from mappers.confluence_mapper import map_page_to_cwi  # noqa: E402
from sync_state import get_last_sync, set_last_sync  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).parent / "storage"
STORAGE_DIR.mkdir(exist_ok=True)
OUTPUT_PATH = STORAGE_DIR / "cwis.json"


def load_existing() -> dict:
    if OUTPUT_PATH.exists():
        return json.loads(OUTPUT_PATH.read_text())
    return {}


def save(cwis_by_id: dict) -> None:
    OUTPUT_PATH.write_text(json.dumps(cwis_by_id, indent=2, default=str))


def build_connector() -> ConfluenceConnector:
    auth = ApiTokenAuth(
        email=os.environ["JIRA_EMAIL"],       # same Atlassian login as Jira
        api_token=os.environ["JIRA_API_TOKEN"],
    )
    return ConfluenceConnector(
        base_url=os.environ["JIRA_URL"],      # same site, Confluence lives under /wiki
        auth=auth,
        space_key=os.environ["CONFLUENCE_SPACE_KEY"],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    connector = build_connector()

    if not connector.test_connection():
        logger.error("Connection test failed — check your .env credentials")
        return

    sync_key = f"confluence:{connector.space_key}"
    since = None if args.full else get_last_sync(sync_key)
    logger.info("Syncing since: %s", since or "(full sync)")

    cwis_by_id = load_existing()
    new_count = 0
    updated_count = 0

    for page in connector.fetch_items(since=since):
        cwi = map_page_to_cwi(page)
        cwi_id = cwi["cwi_id"]
        if cwi_id in cwis_by_id:
            updated_count += 1
        else:
            new_count += 1
        cwis_by_id[cwi_id] = cwi

    save(cwis_by_id)
    set_last_sync(sync_key)

    logger.info(
        "Done. %d new CWIs, %d updated. Total in cwis.json: %d",
        new_count, updated_count, len(cwis_by_id),
    )


if __name__ == "__main__":
    main()