# store_confluence.py
"""
Sync Confluence pages into the Common Substrate.

End-to-end ingestion flow:

Confluence
  -> ConfluenceConnector
  -> Confluence Mapper
  -> CWI
  -> Common Substrate
       -> Artifact Registry
       -> Context Registry
       -> Provenance
       -> Sync History

After ingestion, the Common Substrate builds the
Discovery Agent context package.

Usage:
    python store_confluence.py
    python store_confluence.py --full
"""

import argparse
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from auth import ApiTokenAuth  # noqa: E402
from connectors.confluence_connector import ConfluenceConnector  # noqa: E402
from mappers.confluence_mapper import map_page_to_cwi  # noqa: E402


from substrate.artifact_registry import upsert_artifact  # noqa: E402
from substrate.context_registry import (  # noqa: E402
    register_source,
    record_provenance,
    record_sync,
)
from substrate.models import initialize_database  # noqa: E402
from substrate.context_assembler import build_discovery_package  # noqa: E402
from substrate.db import get_connection  # noqa: E402
from sync_state import get_last_sync, set_last_sync  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)


def build_connector() -> ConfluenceConnector:
    auth = ApiTokenAuth(
        email=os.environ["JIRA_EMAIL"],
        api_token=os.environ["JIRA_API_TOKEN"],
    )

    return ConfluenceConnector(
        base_url=os.environ["JIRA_URL"],
        auth=auth,
        space_key=os.environ["CONFLUENCE_SPACE_KEY"],
    )


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore last-sync state and fetch everything",
    )

    args = parser.parse_args()

    # 1. Initialize Common Substrate database
    logger.info("Initializing database...")

    initialize_database()

    logger.info("Database ready.")

    # 2. Build connector
    connector = build_connector()

    # 3. Test Confluence connection
    if not connector.test_connection():
        logger.error(
            "Confluence connection test failed — check your .env credentials"
        )
        return

    # 4. Register Confluence source
    source_name = f"Confluence space {connector.space_key}"

    source_id = register_source(
        source_type=connector.source_name,
        source_name=source_name,
        source_config={
            "space_key": connector.space_key,
        },
    )

    logger.info(
        "Registered source: %s (%s)",
        source_name,
        source_id,
    )

    # 5. Determine incremental/full sync
    sync_key = f"confluence:{connector.space_key}"

    since = None if args.full else get_last_sync(sync_key)

    logger.info(
        "Syncing Confluence since: %s",
        since or "(full sync)",
    )

    # 6. Fetch → Map → Store
    items_fetched = 0
    items_created = 0
    items_updated = 0

    try:

        for page in connector.fetch_items(since=since):

            items_fetched += 1

            logger.debug(
                "Processing Confluence page %s",
                page.get("id"),
            )

            # Raw Confluence → common CWI
            cwi = map_page_to_cwi(page)

            # Determine whether artifact already exists
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
                    extraction_method="confluence_connector",
                )

        # 7. Record successful sync
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

        logger.info(
            "Discovery Agent package generated: %s",
            package_path,
        )

        logger.info("Confluence ingestion completed.")
        logger.info("Fetched: %d", items_fetched)
        logger.info("Created: %d", items_created)
        logger.info("Updated: %d", items_updated)

    except Exception as exc:

        logger.exception("Confluence ingestion failed.")

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