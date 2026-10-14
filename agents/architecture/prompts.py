# agents/architecture/prompts.py
"""Prompts for the Architecture Discovery & Blueprint Agent."""

import json

ARCHITECTURE_SYSTEM_PROMPT = """
You are the Architecture Discovery & Blueprint Agent in an AI-driven SDLC.

You receive an architecture-specific context package assembled from the
Common Substrate. It includes persisted Candidate Work Items (CWIs),
their supporting source artifacts, and relevant retrieved knowledge.

Your responsibility is to analyze the technical change implied by each CWI
and produce a structured architecture discovery and blueprint draft.

RULES:
1. Never invent existing services, components, APIs, databases, repositories,
   dependencies, or deployment topology.
2. Separate evidence-backed facts from assumptions and recommendations.
3. Use only artifact IDs provided in the context package as evidence.
4. If architecture details are missing, state that they are unknown and
   create open questions instead of fabricating details.
5. Identify affected components, integration points, constraints, assumptions,
   technical risks, and possible architecture options.
6. Explain the pros, cons, and risks of each option.
7. Recommend an option when sufficient context exists. The recommendation
   is advisory; humans make architecture and security decisions.
8. Confidence must be a number between 0 and 1.
9. Set needs_review=true if confidence is below 0.70, evidence is insufficient,
   important assumptions remain, or security impact is unclear.
10. Return valid JSON only. Do not wrap the response in Markdown fences.
"""

def build_architecture_user_prompt(package: dict) -> str:
    schema = {
        "blueprints": [
            {
                "cwi_artifact_id": "string",
                "cwi_id": "string or null",
                "title": "string",
                "problem_statement": "string",
                "current_state": {
                    "known_components": [
                        {
                            "name": "string",
                            "role": "string",
                            "evidence_artifact_ids": [],
                        }
                    ],
                    "known_dependencies": [
                        {
                            "from": "string",
                            "to": "string",
                            "relationship": "string",
                            "evidence_artifact_ids": [],
                        }
                    ],
                    "known_data_flows": [
                        {
                            "description": "string",
                            "evidence_artifact_ids": [],
                        }
                    ],
                    "gaps": [],
                },
                "affected_components": [
                    {
                        "name": "string",
                        "change": "string",
                        "reason": "string",
                        "evidence_artifact_ids": [],
                    }
                ],
                "integration_points": [
                    {
                        "system_or_interface": "string",
                        "change": "string",
                        "evidence_artifact_ids": [],
                    }
                ],
                "constraints": [],
                "assumptions": [],
                "open_questions": [],
                "architecture_options": [
                    {
                        "name": "string",
                        "description": "string",
                        "pros": [],
                        "cons": [],
                        "risks": [],
                    }
                ],
                "recommended_option": "string",
                "architecture_blueprint": {
                    "components": [
                        {
                            "name": "string",
                            "responsibility": "string",
                        }
                    ],
                    "interfaces": [
                        {
                            "name": "string",
                            "purpose": "string",
                            "inputs": [],
                            "outputs": [],
                        }
                    ],
                    "data_flows": [],
                    "deployment_considerations": [],
                    "security_considerations": [],
                },
                "adr_candidates": [
                    {
                        "title": "string",
                        "context": "string",
                        "decision": "string",
                        "consequences": [],
                    }
                ],
                "technical_risks": [
                    {
                        "risk": "string",
                        "impact": "low|medium|high|unknown",
                        "mitigation": "string",
                        "evidence_artifact_ids": [],
                    }
                ],
                "confidence": 0.0,
                "needs_review": True,
                "reasoning_summary": "string",
                "evidence_artifact_ids": [],
            }
        ]
    }

    return (
        "Generate exactly one blueprint for every item in "
        "context.candidate_work_items.\n\n"
        "Evidence IDs must refer only to artifact IDs in context.artifacts "
        "or context.retrieved_chunks. Do not create blueprints for source "
        "artifacts that are not candidate work items. If information is "
        "missing, state the gap and add open questions.\n\n"
        "Return JSON matching this schema:\n"
        + json.dumps(schema, ensure_ascii=False)
        + "\n\nCONTEXT PACKAGE:\n"
        + json.dumps(package, ensure_ascii=False, default=str)
    )