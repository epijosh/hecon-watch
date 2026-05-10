"""Similarity helpers — primarily an ATC-prefix tiebreaker for nearest-neighbour
ranking when Voyage embedding cosine scores are within a small epsilon.

The Voyage embedding cosine over the 25-field PSD decision profile is the
primary similarity signal. ATC similarity here is used only to disambiguate
ties — it's a coarse, deterministic tiebreaker, not the primary ranking signal.
"""

from __future__ import annotations


def atc_prefix_match(a: str | None, b: str | None) -> int:
    """Number of leading characters two ATC codes share.

    ATC codes are 7 characters at the substance level (e.g. A10BJ06 — semaglutide).
    More shared prefix → closer therapeutic relationship:
      1 char  → same anatomical group (A, B, C, D…)
      3 chars → same therapeutic subgroup
      4 chars → same chemical/pharmacological subgroup
      5 chars → same chemical subgroup
      7 chars → same substance
    """
    if not a or not b:
        return 0
    a, b = a.strip().upper(), b.strip().upper()
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def break_score_ties(
    candidates: list[tuple[float, int]],
    indices_to_atc: dict[int, str | None],
    source_atc: str | None,
    epsilon: float = 0.01,
) -> list[tuple[float, int]]:
    """Stable-resort a (score, idx) candidate list so ATC-prefix-matching
    candidates float to the top of any near-tie cluster.

    `candidates` should already be sorted by score descending. We walk it,
    grouping consecutive entries that are within `epsilon` of each other,
    and within each group sort by ATC prefix length to ``source_atc``
    descending. Entries outside the source's ATC reach (no source_atc, or
    no ATC on the candidate) are stable — left in their cosine order.
    """
    if not candidates or not source_atc:
        return candidates

    out: list[tuple[float, int]] = []
    i = 0
    n = len(candidates)
    while i < n:
        # Group runs that are all within epsilon of the head of the group
        head_score = candidates[i][0]
        j = i
        while j < n and (head_score - candidates[j][0]) <= epsilon:
            j += 1
        group = candidates[i:j]
        if len(group) > 1:
            # Sort the group by ATC prefix match descending; preserve
            # original order for entries with equal ATC match (stable sort).
            group = sorted(
                group,
                key=lambda sc_idx: -atc_prefix_match(indices_to_atc.get(sc_idx[1]), source_atc),
            )
        out.extend(group)
        i = j
    return out
