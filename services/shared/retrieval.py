from collections.abc import Sequence


RRF_K = 60.0


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    rank_constant: float = RRF_K,
) -> list[tuple[str, float]]:
    """Fuse ranked identifiers deterministically with reciprocal rank fusion.

    Duplicate identifiers inside one ranking contribute only at their first
    position. Ties retain first-seen order across the supplied rankings and use
    the identifier only as a final deterministic fallback.
    """
    if rank_constant <= 0:
        raise ValueError("RRF rank_constant must be positive")

    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    seen_order = 0

    for ranking in rankings:
        seen_in_ranking: set[str] = set()
        for rank, item_id in enumerate(ranking, start=1):
            normalized_id = str(item_id).strip()
            if not normalized_id or normalized_id in seen_in_ranking:
                continue
            seen_in_ranking.add(normalized_id)
            if normalized_id not in first_seen:
                first_seen[normalized_id] = seen_order
                seen_order += 1
            scores[normalized_id] = scores.get(normalized_id, 0.0) + (
                1.0 / (rank_constant + rank)
            )

    return sorted(
        scores.items(),
        key=lambda item: (-item[1], first_seen[item[0]], item[0]),
    )
