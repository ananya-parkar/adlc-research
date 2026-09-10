# store_cwis.py
"""
Run this to sync issues from Jira into local CWI storage.

Usage:
    cp .env.example .env
    pip install -r requirements.txt
    python store_cwis.py              # incremental sync (only changed issues)
    python store_cwis.py --full       # full sync (ignores last-sync timestamp)
End-to-end ingestion flow:

Jira
  -> JiraConnector
  -> Jira Mapper
  -> CWI
  -> Common Substrate
       -> Artifact Registry
       -> Context Registry
       -> Provenance

Run: python store_cwis.py
or:
    python store_cwis.py --full
"""

import argparse
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from auth import ApiTokenAuth  # noqa: E402
from mappers.jira_mapper import map_issue_to_cwi  # noqa: E402
from connectors.jira_connector import JiraConnector  # noqa: E402
from sync_state import get_last_sync, set_last_sync  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).parent / "storage"
STORAGE_DIR.mkdir(exist_ok=True)
OUTPUT_PATH = STORAGE_DIR / "cwis.json"
from auth import ApiTokenAuth
from connectors.jira_connector import JiraConnector
from mappers.jira_mapper import map_issue_to_cwi

from substrate.artifact_registry import upsert_artifact
from substrate.context_registry import (
    register_source,
    record_provenance,
    record_sync,
)

from substrate.models import initialize_database
from substrate.db import get_connection
from substrate.context_assembler import build_discovery_package
from sync_state import get_last_sync, set_last_sync


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)


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

    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore last-sync state and fetch everything",
    )

    args = parser.parse_args()

    # ---------------------------------------------------------
    # 1. Initialize Common Substrate database
    # ---------------------------------------------------------

    logger.info("Initializing database...")

    initialize_database()

    logger.info("Database ready.")

    # ---------------------------------------------------------
    # 2. Build connector
    # ---------------------------------------------------------

    connector = build_connector()

    # ---------------------------------------------------------
    # 3. Test source connection
    # ---------------------------------------------------------

    if not connector.test_connection():
        logger.error(
            "Jira connection test failed — check your .env credentials"
        )
        return

    # ---------------------------------------------------------
    # 4. Register Jira source
    # ---------------------------------------------------------

    source_name = f"Jira project {connector.project_key}"

    source_id = register_source(
        source_type=connector.source_name,
        source_name=source_name,
        source_config={
            "project_key": connector.project_key,
        },
    )

    logger.info(
        "Registered source: %s (%s)",
        source_name,
        source_id,
    )

    # ---------------------------------------------------------
    # 5. Determine incremental/full sync
    # ---------------------------------------------------------

    sync_key = f"jira:{connector.project_key}"

    since = None if args.full else get_last_sync(sync_key)

    logger.info(
        "Syncing Jira since: %s",
        since or "(full sync)",
    )

    # ---------------------------------------------------------
    # 6. Fetch → Map → Store
    # ---------------------------------------------------------

    items_fetched = 0
    items_created = 0
    items_updated = 0

    try:

        for issue in connector.fetch_items(since=since):

            items_fetched += 1

            logger.debug(
                "Processing Jira issue %s",
                issue.get("key"),
            )

            # Raw Jira → common CWI
            cwi = map_issue_to_cwi(issue)

            # Determine whether artifact already exists
            # based on source + source reference.
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT artifact_id
                        FROM artifacts
                        WHERE source_id = %s
                          AND source_ref = %s
                        """,
                        (
                            source_id,
                            cwi["source_refs"][0],
                        ),
                    )

                    existing = cur.fetchone()

            if existing:
                items_updated += 1
            else:
                items_created += 1

            # CWI → Artifact Registry
            artifact_id = upsert_artifact(
                cwi,
                source_id,
            )

            # Artifact → Provenance
            for source_ref in cwi.get("source_refs", []):

                record_provenance(
                    artifact_id=artifact_id,
                    source_id=source_id,
                    source_ref=source_ref,
                    extraction_method="jira_connector",
                )

        # -----------------------------------------------------
        # 7. Record successful sync
        # -----------------------------------------------------

        record_sync(
            source_id=source_id,
            status="success",
            items_fetched=items_fetched,
            items_created=items_created,
            items_updated=items_updated,
        )

        set_last_sync(sync_key)

        # 8. Build Discovery Agent context package
        package_path = build_discovery_package()

        logger.info("Discovery Agent package generated: %s", package_path,)

        logger.info("Jira ingestion completed.")
        logger.info("Fetched: %d", items_fetched)
        logger.info("Created: %d", items_created)
        logger.info("Updated: %d", items_updated)

    except Exception as exc:

        logger.exception("Jira ingestion failed.")

        record_sync(
            source_id=source_id,
            status="failed",
            items_fetched=items_fetched,
            items_created=items_created,
            items_updated=items_updated,
            error_message=str(exc),
        )

        raise


if __name__ == "__main__":
    main()