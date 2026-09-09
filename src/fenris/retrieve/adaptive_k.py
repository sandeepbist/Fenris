"""Adaptive-k selection (the build plan, exactly).

Given a best-first ranked list, normalize ``s' = s / s_1`` (divide by the top
score), then cut **after rank i** when:

- ELBOW: ``s'_i - s'_{i+1} > ELBOW_DELTA`` (0.15), or
- MASS: cumulative mass ``sum(s'_1..s'_i) >= CUMULATIVE_MASS`` (0.90) - the
 cumulative sum also normalized, ``s''_j = s_j / sum(all scores)``, so "90%
 of total relevance" is measured against the whole candidate list.

Clamp K to [K_MIN, K_MAX] = [1, 25]; the default when nothing triggers is
K_DEFAULT = 8. Input is the full reranked 30 (or the RRF list on the EXACT
no-rerank path); output is the selected prefix.

Convention note: the elbow rule of thumb is documented as "> 0.15" - read as
a strict drop between the retained rank i and the next candidate. All
constants come from fenris.config (ELBOW_DELTA, CUMULATIVE_MASS, K_MIN,
K_MAX, K_DEFAULT).
"""

from __future__ import annotations

from fenris.config import CUMULATIVE_MASS, ELBOW_DELTA, K_DEFAULT, K_MAX, K_MIN


def select_k(ranked: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Select the adaptive-k prefix of a best-first ``[(chunk_id, score)]`` list.

 Returns the selected prefix (K entries, K in [1, 25]). Empty input -> [].
 A single candidate -> [that candidate] (K_MIN clamps to 1, never 0).

 Precedence: whichever rule (elbow or mass) fires first wins; only when
 neither fires across the entire list does the default K (8) apply - the
 tail past 8 near-uniform candidates carries no signal worth its prompt
 cost. The result is then clamped to [K_MIN, K_MAX].
 """
    n = len(ranked)
    if n == 0:
        return []
    if n == 1:
        return list(ranked[:1])

    s1 = ranked[0][1]
    if s1 <= 0:
        # degenerate scores (all zero / negative): fall back to the default K
        k = K_DEFAULT
        return list(ranked[:k])

    # normalized scores for the elbow rule
    norm = [s / s1 for _cid, s in ranked]
    # cumulative mass against the total (also handles negative scores by
    # clamping them to 0 for the mass computation)
    total = sum(s for _cid, s in ranked if s > 0)
    mass = 0.0
    cut = n  # default: keep everything, then clamp
    triggered = False
    for i in range(n - 1):  # i is 0-based rank of a *retained* candidate
        if norm[i] - norm[i + 1] > ELBOW_DELTA:
            cut = i + 1
            triggered = True
            break
        if total > 0:
            if ranked[i][1] > 0:
                mass += ranked[i][1]
            if mass / total >= CUMULATIVE_MASS:
                cut = i + 1
                triggered = True
                break
    if not triggered:
        # neither rule fired across the whole list: the default K applies
        # (the tail past K_DEFAULT carries no signal worth its prompt cost)
        k = min(K_DEFAULT, n)
    else:
        k = min(cut, n)
    k = max(K_MIN, min(K_MAX, k))
    return list(ranked[:k])
