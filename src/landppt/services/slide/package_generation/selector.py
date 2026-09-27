"""Bounded semantic evaluation and deterministic assignment of legal candidates.

TypeSafe protocol: https://docs.typesafe.ai/api (verified 2026-09-23).
"""

import httpx

from .candidate_filter import compatible_components
from .content_service import component_contracts
from .jev_client import JevClient


def assign(
    pages,
    candidates,
    scores,
    *,
    previous_family=None,
    image_budget=0,
    previous_components=(),
    fixed_before=None,
):
    """Beam search over legal candidates; preserve earlier batches and image budget."""
    families = {c.id: c.family for values in candidates.values() for c in values}
    fixed_before = fixed_before or {}
    beam = [(0.0, [], tuple(previous_components[-8:]), previous_family, image_budget)]
    for page in pages:
        next_beam = []
        best = max(
            (scores.get((page.slide_id, c.id), 0) for c in candidates[page.slide_id]),
            default=0,
        )
        for total, chosen, history, last, remaining in beam:
            history = (history + tuple(fixed_before.get(page.slide_id, ())))[-8:]
            if history:
                last = families.get(history[-1], last)
            for component in candidates[page.slide_id]:
                cost = len(page.visual_briefs)
                if cost > remaining:
                    continue
                fit = scores.get((page.slide_id, component.id), 0.0)
                # Relative ranking slack, not a calibrated Jev probability cutoff.
                # Clearly weaker candidates never win merely to create variety.
                if fit < best - 0.15 - 1e-9:
                    continue
                run = 0
                for cid in reversed(history):
                    if cid != component.id:
                        break
                    run += 1
                penalty = 0.14 * run + 0.025 * history.count(component.id)
                if component.family == last:
                    penalty += 0.035
                score = total + fit - penalty
                next_beam.append(
                    (
                        score,
                        chosen + [component.id],
                        (history + (component.id,))[-8:],
                        component.family,
                        remaining - cost,
                    )
                )
        if not next_beam:
            raise ValueError(f"页面 {page.slide_id} 没有符合约束的组件")
        beam = sorted(next_beam, key=lambda item: (-item[0], item[1]))[:16]
    return {
        page.slide_id: component_id for page, component_id in zip(pages, beam[0][1])
    }


async def select_components(
    package,
    pages,
    options,
    config,
    content_service,
    *,
    previous_family=None,
    previous_components=(),
    fixed_before=None,
    locked=None,
    preferred=None,
):
    locked = locked or {}
    # Layouts the outline chose for each page; a tie-breaking preference only.
    preferred = preferred or {}
    candidates = {
        p.slide_id: compatible_components(
            package,
            p,
            allow_images=options.allow_images,
            image_budget=options.image_budget,
            locked_component_id=locked.get(p.slide_id),
        )
        for p in pages
    }
    if any(not value for value in candidates.values()):
        raise ValueError("部分页面没有满足内容容量与图片约束的组件")
    mode = options.selector
    scores, audit = {}, {"selector": "rules", "calls": []}
    if (
        mode in {"auto", "jev"}
        and config.get("jev_enabled")
        and config.get("jev_api_key")
    ):
        try:
            scores, calls = await JevClient(
                config["jev_api_key"],
                config.get("jev_model") or "jev-latest",
                config.get("jev_timeout", 30),
                config.get("jev_retries", 2),
                concurrency=config.get("jev_concurrency", 1),
                endpoint_url=config.get("jev_endpoint_url")
                or "https://api.typesafe.ai/v1/systemone",
                protocol=config.get("jev_protocol") or "jev",
            ).evaluate(pages, candidates)
            audit = {"selector": "jev", "calls": calls}
        except (httpx.HTTPError, ValueError, TypeError):
            audit["fallback_reason"] = "Jev unavailable or invalid response"
    incomplete = [
        p
        for p in pages
        if any((p.slide_id, c.id) not in scores for c in candidates[p.slide_id])
    ]
    if incomplete and mode != "rules":
        import json

        try:
            data, usage = await content_service.json_completion(
                "为每页选择最适合表达目的的合法组件。每一页都必须恰好选中一个组件，"
                "只能从该页的 candidates 中选，不允许留空、返回 null 或自造 ID；"
                "outline_layouts 是大纲阶段为该页选定的版式，合法时优先采用。"
                '返回 {"choices":{slide_id:component_id}}。\n'
                + json.dumps(
                    {
                        "pages": [p.model_dump(mode="json") for p in incomplete],
                        "components": component_contracts(package),
                        "candidates": {
                            sid: [c.id for c in values]
                            for sid, values in candidates.items()
                            if sid in {p.slide_id for p in incomplete}
                        },
                        "outline_layouts": {
                            p.slide_id: preferred.get(p.slide_id)
                            for p in incomplete
                            if preferred.get(p.slide_id)
                        },
                    },
                    ensure_ascii=False,
                )
            )
            choices = data["choices"]
            fallback_scores = {}
            for p in incomplete:
                # An invalid pick for one page no longer discards the others;
                # that page falls back to the rules below and still gets a layout.
                if choices.get(p.slide_id) not in {
                    c.id for c in candidates[p.slide_id]
                }:
                    audit.setdefault("invalid_choices", []).append(p.slide_id)
                    continue
                for c in candidates[p.slide_id]:
                    fallback_scores[p.slide_id, c.id] = (
                        1.0 if c.id == choices[p.slide_id] else rule_score(p, c) * 0.9
                    )
            scores.update(fallback_scores)
            audit["selector"] = "jev+llm" if audit["selector"] == "jev" else "llm"
            audit["fallback_calls"] = [usage]
        except Exception:
            audit["fallback_reason"] = (
                "Semantic selector unavailable; relation-based fallback"
            )
    rule_pages = [
        p
        for p in pages
        if any((p.slide_id, c.id) not in scores for c in candidates[p.slide_id])
    ]
    if rule_pages:
        for p in rule_pages:
            for c in candidates[p.slide_id]:
                scores[p.slide_id, c.id] = rule_score(p, c)
        audit["selector"] = "jev+rules" if audit["selector"] == "jev" else "rules"
        audit["rule_fallback_pages"] = [p.slide_id for p in rule_pages]
    for (sid, cid), value in list(scores.items()):
        if preferred.get(sid) == cid:
            scores[sid, cid] = value + 0.1
    audit["candidate_counts"] = {sid: len(values) for sid, values in candidates.items()}
    audit["single_candidate_pages"] = [
        sid for sid, values in candidates.items() if len(values) == 1
    ]
    return (
        assign(
            pages,
            candidates,
            scores,
            previous_family=previous_family,
            previous_components=previous_components,
            fixed_before=fixed_before,
            image_budget=options.image_budget,
        ),
        audit,
    )


def rule_score(page, component):
    """Deterministic preferences, not model probabilities."""
    preferred = {
        "sequence": "process",
        "comparison": "comparison",
        "evidence": "metrics",
        "summary": "summary",
    }
    target = preferred.get(page.relation, "points")
    if component.family == target:
        return 1.0
    if target in {"points", "summary"} and component.family in {"points", "summary"}:
        return 0.9
    return 0.6
