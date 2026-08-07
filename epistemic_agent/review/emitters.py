"""Translate uncertain upstream cases into ReviewItem records.

Each emitter understands one kind of uncertainty signal and converts it into
the canonical ReviewItem contract. Call `queue.append(item)` to persist.

The first three emitters are domain-agnostic and reusable as-is by any project
built on this framework. The last two are specific to a capability map:
duplicated effort across teams, and multi-value tokens that need splitting.
"""
from __future__ import annotations

import uuid
from typing import Any

from epistemic_agent.review.models import (
    Decision,
    ReviewAttachment,
    ReviewItem,
    ReviewOption,
    ReviewPriority,
    ReviewPrompt,
)


def _new_id() -> str:
    return f"review-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Generic


def emit_grounding_candidate(
    *,
    source_project: str,
    entity_label: str,
    entity_id: str,
    candidates: list[dict[str, Any]],
    cluster_score: float,
    attachment_path: str | None = None,
) -> ReviewItem:
    """A vocabulary hit landed in the 0.50–0.70 uncertainty band."""
    options = [
        ReviewOption(
            id="accept",
            label="Accept top candidate",
            description=candidates[0].get("label", "") if candidates else "",
        ),
        *[
            ReviewOption(
                id=f"pick_{i}",
                label=f"Pick candidate {i + 1}",
                description=c.get("label", ""),
            )
            for i, c in enumerate(candidates[1:3], 1)
        ],
        ReviewOption(id="reject", label="Reject all", description="Leave node ungrounded."),
    ]
    return ReviewItem(
        id=_new_id(),
        source_project=source_project,
        source_type="vocabulary_grounding",
        source_ref={"entity_id": entity_id, "entity_label": entity_label},
        priority=ReviewPriority(
            score=round(1.0 - cluster_score, 3),
            strategy="uncertainty_band",
            reasons=[f"cluster_score={cluster_score:.3f} (0.50–0.70 band)"],
        ),
        prompt=ReviewPrompt(
            title=f"Ground '{entity_label}'?",
            question="Which controlled-vocabulary term best represents this entity?",
            context=[
                f"cluster_score: {cluster_score:.3f}",
                *(
                    f"  [{i + 1}] {c.get('term_id', '')} — {c.get('label', '')}"
                    for i, c in enumerate(candidates[:3])
                ),
            ],
        ),
        options=options,
        suggested_option="accept",
        suggested_confidence=cluster_score,
        attachments=(
            [ReviewAttachment(kind="candidate_table", path=attachment_path)]
            if attachment_path
            else []
        ),
        tags=["vocabulary", "grounding"],
    )


def emit_merge_candidate(
    *,
    source_project: str,
    node_a_id: str,
    node_a_label: str,
    node_b_id: str,
    node_b_label: str,
    similarity: float,
    strategy: str = "fuzzy_label",
) -> ReviewItem:
    """Two nodes may be the same real-world entity."""
    return ReviewItem(
        id=_new_id(),
        source_project=source_project,
        source_type="kg_merge",
        source_ref={"node_a": node_a_id, "node_b": node_b_id},
        priority=ReviewPriority(
            score=round(similarity, 3),
            strategy=strategy,
            reasons=[f"similarity={similarity:.3f}"],
        ),
        prompt=ReviewPrompt(
            title=f"Merge '{node_a_label}' and '{node_b_label}'?",
            question="Are these two nodes the same real-world entity?",
            context=[
                f"A: {node_a_id} — {node_a_label}",
                f"B: {node_b_id} — {node_b_label}",
                f"Similarity: {similarity:.3f} ({strategy})",
            ],
        ),
        options=[
            ReviewOption(
                id="merge_ab",
                label=f"Merge B into A ({node_a_id})",
                description="Keep A as canonical.",
            ),
            ReviewOption(
                id="merge_ba",
                label=f"Merge A into B ({node_b_id})",
                description="Keep B as canonical.",
            ),
            ReviewOption(id="keep_both", label="Keep both", description="They are distinct."),
        ],
        suggested_option="merge_ab" if node_a_id < node_b_id else "merge_ba",
        suggested_confidence=similarity,
        tags=["kg", "merge", "dedup"],
    )


def emit_schema_gap(
    *,
    source_project: str,
    suggested_type: str,
    node_ids: list[str],
    node_labels: list[str],
) -> ReviewItem:
    """Several Untyped nodes cluster around a pattern the schema has no type for."""
    return ReviewItem(
        id=_new_id(),
        source_project=source_project,
        source_type="schema_gap",
        source_ref={"suggested_type": suggested_type, "node_ids": node_ids},
        priority=ReviewPriority(
            score=min(1.0, len(node_ids) / 10),
            strategy="cluster_size",
            reasons=[f"{len(node_ids)} untyped nodes with suggested_type={suggested_type!r}"],
        ),
        prompt=ReviewPrompt(
            title=f"Add schema type '{suggested_type}'?",
            question="Should this cluster become a first-class node type in kg-schema.yaml?",
            context=[f"  • {nid} — {lbl}" for nid, lbl in zip(node_ids[:5], node_labels[:5])],
        ),
        options=[
            ReviewOption(
                id="add_type",
                label=f"Add {suggested_type} to schema",
                description="Promote to first-class type.",
            ),
            ReviewOption(
                id="keep_untyped", label="Keep as Untyped", description="Not enough evidence yet."
            ),
            ReviewOption(
                id="reclassify",
                label="Reclassify under existing type",
                description="Describe in comment.",
            ),
        ],
        tags=["schema", "untyped"],
    )


# ---------------------------------------------------------------------------
# Capability-map specific


def emit_duplicate_effort(
    *,
    source_project: str,
    repo_ids: list[str],
    repo_labels: list[str],
    shared_tech: list[str],
    domain: str,
    jaccard: float,
) -> ReviewItem:
    """Several repos in the same domain share most of their stack.

    This is the finding management actually wants out of a capability map, and
    it is also the one most likely to be wrong — two teams can legitimately
    build LangGraph services for unrelated purposes. Never auto-assert the
    DUPLICATES edge; always route through a human.
    """
    return ReviewItem(
        id=_new_id(),
        source_project=source_project,
        source_type="duplicate_effort",
        source_ref={"repo_ids": repo_ids, "domain": domain, "shared_tech": shared_tech},
        priority=ReviewPriority(
            score=round(min(1.0, jaccard * len(repo_ids) / 3), 3),
            strategy="stack_overlap_in_domain",
            reasons=[
                f"{len(repo_ids)} repos in {domain!r}",
                f"stack Jaccard={jaccard:.2f}",
                f"shared: {', '.join(shared_tech[:6])}",
            ],
        ),
        prompt=ReviewPrompt(
            title=f"Duplicated effort in {domain}?",
            question=(
                "Do these repos solve the same problem, or do they just happen "
                "to share a stack?"
            ),
            context=[
                *(f"  • {rid} — {lbl}" for rid, lbl in zip(repo_ids[:8], repo_labels[:8])),
                f"Shared stack: {', '.join(shared_tech[:10])}",
            ],
        ),
        options=[
            ReviewOption(
                id="confirm_duplicate",
                label="Same problem — assert DUPLICATES",
                description="Consolidation candidate; adds DUPLICATES edges.",
            ),
            ReviewOption(
                id="shared_stack_only",
                label="Shared stack, different problems",
                description="No edge; records the judgement so it is not re-raised.",
            ),
            ReviewOption(
                id="partial",
                label="Partial overlap",
                description="Describe which subset overlaps in the comment.",
            ),
        ],
        suggested_option="shared_stack_only",
        suggested_confidence=jaccard,
        tags=["capability-map", "duplication", domain],
    )


def emit_multi_token(
    *,
    source_project: str,
    node_id: str,
    raw_label: str,
    split_candidates: list[str],
) -> ReviewItem:
    """One label holds several values ('anthropic/openai', 'Chromium/Puppeteer').

    Worth flagging rather than guessing at a split: dependency lists are
    written by hand, so multi-value tokens like this are common, and picking
    the wrong split point silently corrupts two nodes instead of one.
    """
    return ReviewItem(
        id=_new_id(),
        source_project=source_project,
        source_type="multi_token",
        source_ref={"node_id": node_id, "raw_label": raw_label, "splits": split_candidates},
        priority=ReviewPriority(
            score=0.6,
            strategy="multi_value_token",
            reasons=[f"{len(split_candidates)} values in one label"],
        ),
        prompt=ReviewPrompt(
            title=f"Split '{raw_label}'?",
            question="Is this one technology, or several written on one line?",
            context=[f"  → {c}" for c in split_candidates],
        ),
        options=[
            ReviewOption(
                id="split",
                label="Split into separate nodes",
                description=" + ".join(split_candidates),
            ),
            ReviewOption(
                id="keep_whole",
                label="Keep as one",
                description="It genuinely names a single thing.",
            ),
            ReviewOption(
                id="drop", label="Drop", description="Not a technology — extraction noise."
            ),
        ],
        suggested_option="split",
        tags=["capability-map", "multi-token"],
    )


def emit_internal_lockin(
    *,
    source_project: str,
    system_id: str,
    system_label: str,
    dependent_repo_ids: list[str],
) -> ReviewItem:
    """Many repos hang off one internal system — a portability/continuity risk."""
    return ReviewItem(
        id=_new_id(),
        source_project=source_project,
        source_type="internal_lockin",
        source_ref={"system_id": system_id, "repos": dependent_repo_ids},
        priority=ReviewPriority(
            score=round(min(1.0, len(dependent_repo_ids) / 20), 3),
            strategy="dependent_count",
            reasons=[f"{len(dependent_repo_ids)} repos depend on {system_label}"],
        ),
        prompt=ReviewPrompt(
            title=f"{system_label}: {len(dependent_repo_ids)} dependent repos",
            question="Is this concentration acceptable, or does it need a documented exit path?",
            context=[f"  • {rid}" for rid in dependent_repo_ids[:10]],
        ),
        options=[
            ReviewOption(id="accepted", label="Accepted risk", description="Strategic system."),
            ReviewOption(
                id="needs_exit_plan",
                label="Needs exit plan",
                description="Flag for architecture review.",
            ),
            ReviewOption(
                id="mislabelled",
                label="Not actually internal",
                description="Reclassify as TechStack.",
            ),
        ],
        tags=["capability-map", "risk", "lock-in"],
    )


def apply_decision(decision: Decision, kg: dict) -> dict:
    """Apply a logged Decision back to a KG dict.

    Only the decisions whose effect is unambiguous are applied here. Anything
    that needs judgement (which of two IDs survives a merge, how to split a
    multi-token label) is left to the caller, which has the context.
    """
    if decision.decision in ("skip", "defer", "needs_context"):
        return kg
    return kg
