import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_PACKAGE = ROOT / "storage" / "packages" / "discovery_agent_package.json"

# Common sync-state files used by the current connector implementation.
SYNC_STATE_FILES = [
    ROOT / "sync_state.json",
    ROOT / "storage" / "sync_state.json",
]


def header(title: str, char: str = "═") -> None:
    print()
    print(char * 80)
    print(f"  {title}")
    print(char * 80)


def stage(number: int, title: str) -> None:
    print()
    print(f"┌{'─' * 78}┐")
    print(f"│  STEP {number}: {title:<67} │")
    print(f"└{'─' * 78}┘")


def run_command(command: list[str], patterns: list[str]) -> tuple[int, str]:
    """Run command and print only useful lines."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    output = []

    for raw_line in process.stdout or []:
        line = raw_line.rstrip()
        output.append(line)

        if any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns):
            print(f"  • {line}")

    return_code = process.wait()

    if return_code != 0:
        print("\n  ✗ Command failed.")
        print("  Last output:")
        for line in output[-25:]:
            print(f"    {line}")

    return return_code, "\n".join(output)


def extract(pattern: str, text: str, default: str = "—") -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1) if match else default


def clear_database() -> bool:
    """Clear application data while preserving the database schema."""
    print("  • Clearing application data from PostgreSQL...")

    command = [
        "docker",
        "exec",
        "-i",
        "adlc-postgres",
        "psql",
        "-U",
        "adlc_user",
        "-d",
        "adlc",
        "-c",
        (
            "TRUNCATE TABLE "
            "artifact_provenance, "
            "artifacts, "
            "knowledge_chunks, "
            "context_sources, "
            "context_syncs "
            "CASCADE;"
        ),
    ]

    code, _ = run_command(command, [r"TRUNCATE TABLE"])

    if code == 0:
        print("  ✓ PostgreSQL application data cleared")
        return True

    return False


def clear_local_state() -> None:
    """Remove local connector sync state and generated Discovery package."""
    found_state = False

    for state_file in SYNC_STATE_FILES:
        if state_file.exists():
            state_file.unlink()
            print(f"  ✓ Removed {state_file.relative_to(ROOT)}")
            found_state = True

    if not found_state:
        print("  • No local sync-state file found")

    if DEFAULT_PACKAGE.exists():
        DEFAULT_PACKAGE.unlink()
        print("  ✓ Removed old discovery_agent_package.json")
    else:
        print("  • No old Discovery package found")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Jira + Confluence + DOCX -> Discovery Agent E2E flow."
    )
    parser.add_argument(
        "--project-id",
        default="KAN",
        help="Project ID used for Jira and DOCX ingestion. Default: KAN",
    )
    parser.add_argument(
        "--file",
        default="test_requirements.docx",
        help="DOCX file to ingest. Default: test_requirements.docx",
    )
    args = parser.parse_args()

    file_path = ROOT / args.file

    header("ADLC RESEARCH — FULL MULTI-SOURCE DISCOVERY E2E")

    print(
        """
  Fresh run:

  Clear DB + Sync State
          ↓
       Jira FULL
          ↓
    Confluence FULL
          ↓
      DOCX / File
          ↓
    Common Substrate
          ↓
  Discovery Context Package
          ↓
    Discovery Agent
          ↓
 Candidate Work Items + Patterns
"""
    )

    # ------------------------------------------------------------------
    # STEP 1 — Fresh state
    # ------------------------------------------------------------------
    stage(1, "RESET TO A FRESH STATE")

    if not clear_database():
        print("\n✗ Could not clear PostgreSQL data. Stopping.")
        return 1

    clear_local_state()

    # ------------------------------------------------------------------
    # STEP 2 — Jira
    # ------------------------------------------------------------------
    stage(2, "JIRA FULL INGESTION")

    jira_code, jira_output = run_command(
        [sys.executable, "store_jira.py", "--full"],
        [
            r"Jira connection OK",
            r"Registered source",
            r"Syncing Jira",
            r"Fetched \d+ issues",
            r"Jira ingestion completed",
            r"Fetched:",
            r"Created:",
            r"Updated:",
        ],
    )

    if jira_code != 0:
        print("\n✗ E2E flow stopped at Jira ingestion.")
        return jira_code

    jira_fetched = extract(r"Fetched:\s*(\d+)", jira_output, "0")
    jira_created = extract(r"Created:\s*(\d+)", jira_output, "0")
    jira_updated = extract(r"Updated:\s*(\d+)", jira_output, "0")

    if jira_fetched == "0":
        print("\n✗ Jira FULL ingestion returned 0 issues.")
        return 1

    print("\n  ✓ Jira ingestion completed")
    print(f"    Issues fetched: {jira_fetched}")
    print(f"    Created:        {jira_created}")
    print(f"    Updated:        {jira_updated}")

    # ------------------------------------------------------------------
    # STEP 3 — Confluence
    # ------------------------------------------------------------------
    stage(3, "CONFLUENCE FULL INGESTION")

    confluence_code, confluence_output = run_command(
        [sys.executable, "store_confluence.py", "--full"],
        [
            r"Confluence connection OK",
            r"Registered source",
            r"Syncing Confluence",
            r"Fetched \d+",
            r"Fetched:",
            r"Created:",
            r"Updated:",
            r"Stored",
            r"ingestion completed",
            r"complete",
        ],
    )

    if confluence_code != 0:
        print("\n✗ E2E flow stopped at Confluence ingestion.")
        return confluence_code

    confluence_fetched = extract(
        r"Fetched:\s*(\d+)",
        confluence_output,
        "—",
    )

    print("\n  ✓ Confluence ingestion completed")
    if confluence_fetched != "—":
        print(f"    Items fetched: {confluence_fetched}")

    # ------------------------------------------------------------------
    # STEP 4 — DOCX
    # ------------------------------------------------------------------
    stage(4, "DOCX / FILE INGESTION")

    if not file_path.exists():
        print(f"\n✗ File not found: {file_path}")
        return 1

    print(f"  • File:    {file_path.name}")
    print(f"  • Project: {args.project_id}")

    file_code, file_output = run_command(
        [
            sys.executable,
            "store_file.py",
            "--file",
            str(file_path),
            "--project-id",
            args.project_id,
        ],
        [
            r"Stored",
            r"Created",
            r"Updated",
            r"artifact",
            r"ingestion",
            r"complete",
            r"source",
        ],
    )

    if file_code != 0:
        print("\n✗ E2E flow stopped at DOCX ingestion.")
        return file_code

    print("\n  ✓ DOCX ingestion completed")

    # ------------------------------------------------------------------
    # STEP 5 — Discovery Agent
    # ------------------------------------------------------------------
    stage(5, "DISCOVERY AGENT")

    discovery_code, discovery_output = run_command(
        [
            sys.executable,
            "-m",
            "agents.discovery.discovery_agent",
        ],
        [
            r"Package built at",
            r"Package contains",
            r"Discovery Agent produced",
            r"Created/updated CWI",
            r"Discovery complete",
            r"DISCOVERY PATTERN",
        ],
    )

    if discovery_code != 0:
        print("\n✗ E2E flow stopped at Discovery Agent.")
        return discovery_code

    package_count = extract(
        r"Package contains\s+(\d+)\s+total artifacts",
        discovery_output,
    )
    source_count = extract(
        r"Package contains\s+\d+\s+total artifacts;\s+(\d+)\s+source artifacts",
        discovery_output,
    )
    cwi_count = extract(
        r"Discovery Agent produced\s+(\d+)\s+candidate work items",
        discovery_output,
    )
    pattern_count = extract(
        r"Discovery Agent produced\s+\d+\s+candidate work items and\s+(\d+)\s+patterns",
        discovery_output,
    )

    cwi_lines = [
        line
        for line in discovery_output.splitlines()
        if "Created/updated CWI" in line
    ]

    pattern_lines = [
        line
        for line in discovery_output.splitlines()
        if "DISCOVERY PATTERN:" in line
    ]

    print("\n  ✓ Discovery Agent completed")

    print("\n  Discovery Summary")
    print("  " + "─" * 72)
    print(f"  • Total package artifacts : {package_count}")
    print(f"  • Source artifacts        : {source_count}")
    print(f"  • Candidate Work Items    : {cwi_count}")
    print(f"  • Patterns detected      : {pattern_count}")

    if cwi_lines:
        print("\n  Candidate Work Items")
        print("  " + "─" * 72)
        for line in cwi_lines:
            message = line.split("INFO ", 1)[-1]
            print(f"  • {message}")

    if pattern_lines:
        print("\n  Discovery Patterns")
        print("  " + "─" * 72)
        for line in pattern_lines:
            message = line.split("DISCOVERY PATTERN:", 1)[-1].strip()
            print(f"  • {message}")

    # ------------------------------------------------------------------
    # FINAL SUMMARY
    # ------------------------------------------------------------------
    header("FULL MULTI-SOURCE E2E FLOW COMPLETED", "─")

    print(
        f"""
  ✓ Fresh PostgreSQL state
  ✓ Jira FULL ingestion
  ✓ Confluence FULL ingestion
  ✓ DOCX ingestion
  ✓ Common Substrate populated
  ✓ Discovery package built   ({source_count} source artifacts)
  ✓ Discovery Agent executed
  ✓ Candidate Work Items      ({cwi_count})
  ✓ Discovery patterns        ({pattern_count})

  End-to-end:

  Jira ──────────────┐
                     │
  Confluence ────────┼──→ Common Substrate
                     │          ↓
  DOCX / File ───────┘   Discovery Context
                              ↓
                       Discovery Agent
                              ↓
                   Candidate Work Items
                         + Patterns
"""
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
