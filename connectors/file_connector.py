"""
File connector — handles user-uploaded PDF/DOCX documents (requirement
docs, specs, anything not already in Jira/Confluence).

This is deliberately NOT a SourceConnector subclass like Jira/ADO/
Confluence. Those are pull-based: given credentials, they reach out
and fetch. This one is push-based: the user hands you a file directly,
there's nothing to "fetch" or paginate. Forcing it into the same
interface would just mean fake-implementing test_connection() and
fetch_items() for no benefit — so it stays a plain module of
extraction functions instead.
"""

import logging
from pathlib import Path

import pdfplumber
from docx import Document

logger = logging.getLogger(__name__)


def extract_text_from_pdf(path: Path) -> str:
    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n\n".join(text_parts)


def extract_text_from_docx(path: Path) -> str:
    doc = Document(path)
    return "\n".join(para.text for para in doc.paragraphs if para.text.strip())


def extract_text(path: Path) -> str:
    """Dispatches to the right extractor based on file extension."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(path)
    elif suffix in (".docx", ".doc"):
        if suffix == ".doc":
            raise ValueError(
                "Legacy .doc format isn't supported — only .docx. "
                "Ask the user to re-save as .docx, or convert it first."
            )
        return extract_text_from_docx(path)
    else:
        raise ValueError(f"Unsupported file type: {suffix} — only .pdf and .docx are supported")