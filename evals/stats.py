"""Small statistics helpers (standard library only)."""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

Z95 = 1.959963984540054


def wilson_interval(k: int, n: int, z: float = Z95) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion. (nan, nan) when n == 0."""
    if n <= 0:
        return (float("nan"), float("nan"))
    if not 0 <= k <= n:
        raise ValueError("need 0 <= k <= n")
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    lo, hi = centre - half, centre + half
    # clamp float noise at the boundaries
    return (0.0 if k == 0 else max(0.0, lo), 1.0 if k == n else min(1.0, hi))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts.

    ``b`` = pairs where only the first condition succeeded, ``c`` = pairs where
    only the second did. Under H0 the discordant pairs split 50/50, so the
    p-value is a two-sided exact binomial test with p = 0.5.
    """
    if b < 0 or c < 0:
        raise ValueError("counts must be non-negative")
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired_counts(a: Dict[object, bool], b: Dict[object, bool]) -> Dict[str, int]:
    """Contingency counts over keys present in both mappings."""
    out = {"both": 0, "only_a": 0, "only_b": 0, "neither": 0}
    for key in a.keys() & b.keys():
        x, y = a[key], b[key]
        if x and y:
            out["both"] += 1
        elif x:
            out["only_a"] += 1
        elif y:
            out["only_b"] += 1
        else:
            out["neither"] += 1
    return out


def cohens_kappa(labels_a: Sequence[str], labels_b: Sequence[str]) -> Optional[float]:
    """Cohen's kappa for two raters over the same items (None if undefined)."""
    if len(labels_a) != len(labels_b) or not labels_a:
        return None
    n = len(labels_a)
    cats = sorted(set(labels_a) | set(labels_b))
    po = sum(1 for x, y in zip(labels_a, labels_b) if x == y) / n
    pe = sum((labels_a.count(c) / n) * (labels_b.count(c) / n) for c in cats)
    if pe == 1.0:
        return None
    return (po - pe) / (1 - pe)
