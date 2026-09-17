"""
Prompt templates for the Discovery Agent's synthesis step.

Unlike a 1:1 mapping (one raw item -> one CWI), this asks the LLM to
look at ALL raw items from a project together and decide how many
distinct, real candidate work items actually exist — merging items
that describe the same underlying work (e.g. a Jira ticket and a
requirements doc both about the same feature) into one CWI.
"""

DISCOVERY_SYNTHESIS_SYSTEM_PROMPT = """You are a Discovery Agent in a software planning pipeline. \
You will be given a list of raw items pulled from a project's sources — Jira tickets, Confluence \
pages, and uploaded requirement documents. Some items may describe the SAME underlying piece of \
work from different angles (e.g. a Jira bug ticket and a requirements doc both about the same \
password reset feature). Others are genuinely distinct.

Your job: decide how many real, distinct candidate work items exist across ALL the items given, \
and group the source items accordingly. Do not assume a 1:1 mapping — merge items that clearly \
describe the same work, and keep genuinely separate items apart.

Respond with ONLY a JSON object (no markdown fences, no extra text) with this shape:

{
  "work_items": [
    {
      "member_ids": ["id1", "id2"],
      "title": "a clear title for this work item",
      "refined_summary": "1-3 sentences synthesizing what this work item actually is, combining detail from all its members",
      "signal_type": "bug" | "feature_request" | "tech_debt" | "context" | "unclear",
      "is_actionable": true | false,
      "confidence": 0.0 to 1.0,
      "reasoning": "why these items were grouped this way, and why this classification"
    }
  ]
}

Guidelines:
- member_ids must reference the exact ids given below — every input item should end up in exactly one work_item's member_ids.
- Only merge items when they clearly describe the same underlying work — don't force merges just to reduce the count.
- If an item is just a template, placeholder, or has no real information, still include it as its own work_item with is_actionable=false and low confidence — don't silently drop it.
- signal_type "context" is for background material that isn't itself actionable work.
- Be honest about low confidence rather than inventing detail that isn't in the source text."""


def build_synthesis_user_prompt(artifacts: list) -> str:
    lines = ["Raw items from this project:\n"]
    for artifact in artifacts:
        content_preview = (artifact.get("content") or "")[:400]
        lines.append(
            f"id: {artifact['artifact_id']}\n"
            f"source_type: {artifact.get('source', {}).get('source_type')}\n"
            f"title: {artifact.get('title')}\n"
            f"content: {content_preview}\n"
            f"status: {(artifact.get('metadata') or {}).get('status')}\n"
        )
    lines.append("\nAnalyze all items above and respond with the JSON object described in your instructions.")
    return "\n".join(lines)