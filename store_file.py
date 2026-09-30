"""
Ingest a single uploaded document (PDF/DOCX) into the substrate,
tagged with a project_id so it links up with Jira/Confluence data
from the same project.

project_id is denormalized directly onto both the artifact row
(artifacts.metadata) and every embedded chunk (knowledge_chunks.metadata)
— no JOIN needed later to filter by project.

The full extracted text is chunked and embedded into the vector
store — artifacts.content only holds a short preview.

Usage:
    python store_file.py --file path/to/requirements.pdf --project-id KAN
"""

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from connectors.file_connector import extract_text  # noqa: E402
from mappers.file_mapper import map_document_to_cwi  # noqa: E402
from substrate.artifact_registry import upsert_artifact  # noqa: E402
from substrate.context_registry import record_provenance, register_source  # noqa: E402
from substrate.vector_store import store_chunks  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to the PDF or DOCX file")
    parser.add_argument("--project-id", required=True, help="Project ID this document belongs to (e.g. Jira project key)")
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        logger.error("File not found: %s", file_path)
        return

    logger.info("Extracting text from %s", file_path.name)
    full_text = extract_text(file_path)
    if not full_text.strip():
        logger.warning("No text extracted from %s — file may be scanned/image-based", file_path.name)

    cwi = map_document_to_cwi(file_path.name, full_text, args.project_id)

    source_id = register_source(
        source_type="file_upload",
        source_name=f"uploads:{args.project_id}",
        source_config={"project_id": args.project_id},
    )

    artifact_id = upsert_artifact(
        cwi=cwi,
        source_id=source_id,
        artifact_type="source_artifact",
        project_id=args.project_id,
    )

    record_provenance(
        artifact_id=artifact_id,
        source_id=source_id,
        source_ref=file_path.name,
        extraction_method="file_upload",
    )

    chunk_count = store_chunks(
        artifact_id,
        full_text,
        project_id=args.project_id,
        metadata={"filename": file_path.name},
    )

    logger.info(
        "Stored artifact_id=%s for %s (project_id=%s, %d chunks embedded)",
        artifact_id, file_path.name, args.project_id, chunk_count,
    )


if __name__ == "__main__":
    main()