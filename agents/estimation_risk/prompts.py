# agents/estimation_risk/prompts.py

"""Prompts for the Estimation & Risk Agent."""

import json
from typing import Any


SYSTEM_PROMPT = """
You are an Estimation and Risk Agent in an Agentic Development
Lifecycle (ADLC).

Your responsibility is to estimate already-discovered and specified
work items and identify technical, delivery, dependency, and schedule
risks. Do not rediscover the project or redesign its architecture.

ESTIMATION METHOD
1. Estimate each supplied work item bottom-up at story/work-item level.
2. Provide three effort values in person-hours:
   - optimistic_hours: favorable but plausible conditions
   - most_likely_hours: the most probable effort
   - pessimistic_hours: adverse but plausible conditions
3. Ensure optimistic_hours <= most_likely_hours <= pessimistic_hours.
4. Consider implementation, testing, integration, code review, and
   expected rework. Do not count the same activity twice.
5. Do not add a blanket risk percentage to estimates.
6. Do not calculate PERT expected hours. The application calculates
   them deterministically.
7. Effort means person-hours of work, not calendar duration.
8. Do not derive delivery dates unless explicit scheduling information
   is supplied. Identify schedule constraints and uncertainty instead.
9. Do not silently treat missing information as confirmed.

RISK ASSESSMENT
Identify risks in these categories:
- technical: unfamiliar technology, architecture uncertainty, quality,
  performance, security, or implementation complexity
- delivery: scope volatility, unclear acceptance criteria, resourcing,
  testing readiness, or release/process constraints
- dependency: upstream work, external systems, APIs, teams, approvals,
  or prerequisites
- schedule: sequencing, critical-path uncertainty, deadlines,
  resource availability, or likely blockers

For each risk, state its evidence from the input, likelihood, impact,
and a practical mitigation. Do not invent facts or claim a dependency
is confirmed unless the input supports it. Use an explicit assumption
or open question where evidence is incomplete.


CONFIDENCE AND REVIEW

- high: supplied evidence supports the scope, acceptance criteria,
  and implementation approach with no material unresolved unknowns
- medium: some uncertainty remains, but its likely effect on effort
  and scope is bounded
- low: material uncertainty remains and could significantly change
  effort, scope, dependencies, or technical approach

Resolve questions from supplied evidence before flagging them.
An assumption is not automatically unresolved simply because it
appears in the input.

Set needs_review=true only when a material uncertainty, conflict,
or unverified dependency remains after examining all supplied
context and could materially affect the estimate or scope.

Do not mark a story for review solely because its estimate has a
wide range or because a non-material detail is unknown.
Never mark an unresolved question as resolved without evidence.
Explain remaining material uncertainty in review_reasons.

Return only valid JSON matching the requested output structure.
Do not include Markdown fences or explanatory text outside the JSON.
"""




def build_estimation_prompt(
    context_package: dict[str, Any],
) -> str:
    """Build an estimation prompt using all relevant supplied evidence."""

    context = context_package.get("context", context_package)
    work_items = context.get("work_items_for_estimation", [])

    compact_items = []

    for item in work_items:
        compact_items.append({
            "work_item_id": item.get("work_item_id"),
            "title": item.get("title"),
            "source_kind": item.get("source_kind"),
            "summary": item.get("summary"),
            "acceptance_criteria": item.get("acceptance_criteria", []),
            "blueprint": item.get("blueprint"),
            "dependencies": item.get("dependencies", []),
            "related": item.get("related", []),
            "gaps": item.get("gaps", []),
            "conflicts": item.get("conflicts", []),
            "decisions": item.get("decisions", []),
            "open_questions": item.get("open_questions", []),
            "source_refs": item.get("source_refs", []),
        })

    serialized_context = json.dumps(
        {
            "project_id": context.get("project_id"),
            "architecture_baseline": context.get("architecture_baseline"),
            "work_items_for_estimation": compact_items,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )

    return f"""
Estimate the supplied work items using bottom-up three-point estimation.

CONTEXT RESOLUTION — FOLLOW THIS BEFORE ESTIMATING:

1. Read the entire supplied story, acceptance criteria, architecture
   blueprint, project baseline, dependencies, decisions, gaps,
   conflicts, open questions, and source references.

2. Resolve an assumption only when the supplied evidence supports the
   answer. Reuse confirmed requirements and architecture details.
   Do not ask questions that are already answered by the supplied
   context.

3. Do not treat an architecture recommendation as an implemented fact.
   Do not treat an assumption from an earlier estimate as verified
   evidence. Do not infer that an external account, credential,
   environment, approval, or integration is available unless the
   input confirms it.

4. If evidence conflicts, is incomplete, or does not answer a question,
   preserve the uncertainty. Do not invent values or silently choose
   between conflicting requirements.

5. Include only material unresolved assumptions and questions in the
   returned assumptions, review_reasons, and open_questions. Do not
   flag a story merely because it contains an assumption that the
   supplied evidence has already resolved.

6. Set needs_review=true when a material unresolved question,
   conflicting requirement, unverified dependency, or technical
   uncertainty could materially change the estimate or scope.
   Otherwise set needs_review=false if the available evidence supports
   a sufficiently reliable estimate.

7. Set confidence according to the remaining material uncertainty,
   not merely because some context fields are absent. If the
   implementation approach or scope remains materially unknown,
   use low confidence and explain why.

ESTIMATION:
- Return optimistic_hours, most_likely_hours, pessimistic_hours.
- Ensure optimistic_hours <= most_likely_hours <= pessimistic_hours.
- Use person-hours, including implementation, testing, integration,
  code review, and reasonable rework without double-counting.
- Do not calculate PERT expected effort; Python calculates it.
- Estimate only the supplied work item and preserve its ID exactly.

Return one valid JSON object with exactly these top-level fields:
- work_item_estimates
- risk_register
- overall_confidence
- project_assumptions
- open_questions

For each work item, return:
- work_item_id
- title
- optimistic_hours
- most_likely_hours
- pessimistic_hours
- complexity: low, medium, high, or unknown
- assumptions: list of remaining material assumptions only
- dependencies: list of dependencies supported by the input
- confidence: low, medium, or high
- needs_review: boolean
- review_reasons: list of material unresolved reasons only

Because this request estimates one work item:
- risk_register: []
- project_assumptions: []
- open_questions: []

Do not invent facts. Return valid JSON only.

INPUT:
{serialized_context}
"""


