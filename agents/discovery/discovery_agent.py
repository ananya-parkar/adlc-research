# agents/discovery/discovery_agent.py
"""
Planning Discovery Agent

For now this agent ONLY receives and reads its context package.

It does not:
    - connect to Jira
    - query PostgreSQL
    - perform discovery reasoning
    - call an LLM

Those responsibilities come later.
"""

import json
from pathlib import Path
from typing import Any, Dict

PACKAGE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "storage"
    / "packages"
    / "discovery_agent_package.json"
)


def load_context_package() -> Dict[str, Any]:
    """
    Read the context package prepared by the Common Substrate.
    """

    if not PACKAGE_PATH.exists():
        raise FileNotFoundError(
            f"Discovery Agent package not found: {PACKAGE_PATH}"
        )

    return json.loads(
        PACKAGE_PATH.read_text(encoding="utf-8")
    )


def main() -> None:

    package = load_context_package()
    package_metadata = package["package"]
    context = package["context"]

    print("\n========== DISCOVERY AGENT ==========")

    print(
        f"Package type    : "
        f"{package_metadata['package_type']}"
    )

    print(
        f"Package version : "
        f"{package_metadata['package_version']}"
    )

    print(
        f"Target agent    : "
        f"{package_metadata['target_agent']}"
    )

    print(
        f"Generated at    : "
        f"{package_metadata['generated_at']}"
    )

    print(
        f"\nSources received: "
        f"{len(context['sources'])}"
    )

    for source in context["sources"]:
        print(
            f"  - {source['source_type']} | "
            f"{source['source_name']}"
        )

    print(
        f"\nArtifacts received: "
        f"{context['artifact_count']}"
    )

    for artifact in context["artifacts"]:
        print(
            f"  - {artifact['source']['source_ref']} | "
            f"{artifact['title']}"
        )

    print(
        f"\nProvenance records: "
        f"{len(context['provenance'])}"
    )

    print("\n=====================================")


if __name__ == "__main__":
    main()