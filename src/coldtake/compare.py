"""Agreement map. Two paths and no lexical shortcut:

- options path: deterministic, clusters advisors by their `choice`;
- judge path: a tool-free comparison call whose output is validated here.

If the judge cannot run, the comparison is `unavailable`; `material` is then absent,
never a guess. The unique-assumption and forecast diffs are labelled heuristic.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Any

from .prompts import label_for
from .schemas import JudgeOutput, Packet, Sealed

MemoRecord = dict[str, Any]  # {"id", "family", "provider", "model", "memo", "memo_sha256"}


def unavailable(cause: str, detail: dict[str, str] | None = None) -> dict[str, Any]:
    return {"status": "unavailable", "cause": cause, "cause_detail": detail}


def not_applicable(cause: str) -> dict[str, Any]:
    return {"status": "not_applicable", "cause": cause}


def overall_relation(relations: list[str]) -> str:
    if all(r == "agree" for r in relations):
        return "agree"
    if all(r == "conflict" for r in relations):
        return "conflict"
    return "partial"


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def heuristic_diffs(memos: list[MemoRecord]) -> dict[str, Any]:
    """Cheap set differences between memos. Wording-sensitive, so advisory only."""
    if len(memos) < 2:
        return {"labelled": "heuristic"}

    def field_sets(name: str) -> dict[str, set[str]]:
        return {m["id"]: {_norm(x) for x in m["memo"][name]} for m in memos}

    assumptions = field_sets("assumptions")
    shared = set.intersection(*assumptions.values())
    out: dict[str, Any] = {
        "labelled": "heuristic",
        "unique_assumptions": {k: sorted(v - shared) for k, v in assumptions.items() if v - shared},
    }
    for name in ("forecasts", "feasibility"):
        sets = field_sets(name)
        everything = set.union(*sets.values())
        differing = {k: sorted(everything - v) for k, v in sets.items() if everything - v}
        if differing:
            out[f"{name}_not_mentioned_by"] = differing
    return out


# -- options path --------------------------------------------------------------


def options_comparison(packet: Packet, memos: list[MemoRecord], sealed: Sealed) -> dict[str, Any]:
    labels = {o.id: o.label for o in packet.options or []}
    groups: dict[str, list[str]] = defaultdict(list)
    for record in memos:
        groups[record["memo"]["choice"]].append(record["id"])
    clusters = [
        {"choice": choice, "label": labels.get(choice, "none"), "members": ids}
        for choice, ids in groups.items()
    ]
    choice_of = {m["id"]: m["memo"]["choice"] for m in memos}
    ids = [m["id"] for m in memos]
    pairwise = [
        {"a": a, "b": b, "relation": "agree" if choice_of[a] == choice_of[b] else "conflict"}
        for i, a in enumerate(ids)
        for b in ids[i + 1 :]
    ]
    result: dict[str, Any] = {
        "status": "ok",
        "method": "options",
        "clusters": clusters,
        "pairwise": pairwise,
        "material": len(clusters) > 1,
        "heuristic": heuristic_diffs(memos),
        "sealed": None,
    }
    if sealed.choice is not None:
        per_memo = {i: "agree" if choice_of[i] == sealed.choice else "conflict" for i in ids}
        result["sealed"] = {
            "relation": overall_relation(list(per_memo.values())),
            "per_memo": per_memo,
            "sealed_choice": sealed.choice,
        }
    elif sealed.answer is not None:
        result["sealed"] = unavailable("an options packet needs sealed_choice to compare")
    return result


# -- judge path ----------------------------------------------------------------


def judge_comparison(
    output: JudgeOutput, order: list[int], memos: list[MemoRecord], has_sealed: bool
) -> dict[str, Any]:
    """Validate judge output against the memos it was given and map labels to memo ids.
    Raises ValueError if the output is inconsistent (the caller treats that as a failed
    attempt)."""
    by_label = {label_for(i): memos[index]["id"] for i, index in enumerate(order)}
    memo_labels = set(by_label)
    known = memo_labels | ({"S"} if has_sealed else set())

    def name(label: str) -> str:
        return "sealed" if label == "S" else by_label[label]

    flat = [label for cluster in output.clusters for label in cluster]
    if sorted(flat) != sorted(memo_labels) or any(not c for c in output.clusters):
        raise ValueError("clusters must contain each memo label exactly once")
    # A stance for S is optional (judges reasonably describe it); memo stances are required.
    position_labels = [p.label for p in output.positions if not (has_sealed and p.label == "S")]
    if sum(1 for p in output.positions if p.label == "S") > 1:
        raise ValueError("positions lists S more than once")
    if sorted(position_labels) != sorted(memo_labels):
        raise ValueError("positions must contain exactly one entry per memo label")
    pairs: dict[frozenset[str], Any] = {}
    for pair in output.pairwise:
        key = frozenset((pair.a, pair.b))
        if pair.a not in known or pair.b not in known or pair.a == pair.b:
            raise ValueError("pairwise refers to invalid labels")
        if key in pairs:
            raise ValueError("pairwise repeats a pair")
        pairs[key] = pair
    expected = {frozenset(pair) for pair in combinations(sorted(known), 2)}
    if set(pairs) != expected:
        raise ValueError("pairwise must list every pair of positions exactly once")
    for contradiction in output.contradictions:
        if not set(contradiction.labels) <= known or len(set(contradiction.labels)) != len(
            contradiction.labels
        ):
            raise ValueError("contradiction labels must be known and not repeated")

    sealed: dict[str, Any] | None = None
    if has_sealed:
        if output.sealed is None:
            raise ValueError("judge omitted the sealed comparison")
        per_memo = {by_label[label]: pairs[frozenset((label, "S"))].relation for label in by_label}
        sealed = {
            # Recomputed from the per-memo relations, not taken on trust from the judge.
            "relation": overall_relation(list(per_memo.values())),
            "crux": output.sealed.crux,
            "per_memo": per_memo,
        }
    memo_pairs = [p for p in output.pairwise if p.a in memo_labels and p.b in memo_labels]
    return {
        "status": "ok",
        "method": "judge",
        "positions": [{"memo": name(p.label), "stance": p.stance} for p in output.positions],
        "clusters": [{"members": [by_label[x] for x in c]} for c in output.clusters],
        "pairwise": [
            {"a": name(p.a), "b": name(p.b), "relation": p.relation, "crux": p.crux}
            for p in output.pairwise
        ],
        "sealed": sealed,
        "contradictions": [
            {"claim": c.claim, "memos": [name(x) for x in c.labels]} for c in output.contradictions
        ],
        # Computed here from the validated structure, not taken on trust from the judge.
        "material": len(output.clusters) > 1 or any(p.relation == "conflict" for p in memo_pairs),
        "heuristic": heuristic_diffs(memos),
    }


def sealed_rows(comparison: dict[str, Any], memos: list[MemoRecord]) -> list[tuple[str, str, str]]:
    """(memo_id, family, relation) rows for stats, when a sealed comparison succeeded."""
    sealed = comparison.get("sealed") if comparison.get("status") == "ok" else None
    if not sealed or "per_memo" not in sealed:
        return []
    family = {m["id"]: m["family"] for m in memos}
    return [(i, family[i], rel) for i, rel in sealed["per_memo"].items()]
