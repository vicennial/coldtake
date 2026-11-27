import math

import pytest

from evals.stats import cohens_kappa, mcnemar_exact, paired_counts, wilson_interval


@pytest.mark.parametrize(
    "k,n,lo,hi",
    [
        (0, 10, 0.0, 0.2775),
        (5, 10, 0.2366, 0.7634),
        (10, 10, 0.7225, 1.0),
        (1, 10, 0.0179, 0.4045),
        (36, 36, 0.9035, 1.0),
        (0, 6, 0.0, 0.3903),
    ],
)
def test_wilson_known_values(k, n, lo, hi):
    got_lo, got_hi = wilson_interval(k, n)
    assert got_lo == pytest.approx(lo, abs=5e-4)
    assert got_hi == pytest.approx(hi, abs=5e-4)


def test_wilson_edge_cases():
    lo, hi = wilson_interval(0, 0)
    assert math.isnan(lo) and math.isnan(hi)
    with pytest.raises(ValueError):
        wilson_interval(5, 4)
    lo, hi = wilson_interval(3, 7)
    assert 0 < lo < 3 / 7 < hi < 1


@pytest.mark.parametrize(
    "b,c,p",
    [
        (0, 0, 1.0),
        (0, 5, 0.0625),
        (5, 0, 0.0625),
        (5, 5, 1.0),
        (1, 9, 0.021484375),
        (3, 7, 0.34375),
        (2, 2, 1.0),
        (0, 1, 1.0),
    ],
)
def test_mcnemar_exact(b, c, p):
    assert mcnemar_exact(b, c) == pytest.approx(p)


def test_mcnemar_symmetry_and_range():
    for b in range(8):
        for c in range(8):
            assert mcnemar_exact(b, c) == mcnemar_exact(c, b)
            assert 0.0 <= mcnemar_exact(b, c) <= 1.0


def test_paired_counts_uses_shared_keys_only():
    a = {"t1": True, "t2": True, "t3": False, "t4": False, "only_a": True}
    b = {"t1": True, "t2": False, "t3": True, "t4": False, "only_b": True}
    assert paired_counts(a, b) == {"both": 1, "only_a": 1, "only_b": 1, "neither": 1}


def test_kappa():
    assert cohens_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == pytest.approx(1.0)
    assert cohens_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == pytest.approx(0.0)
    assert cohens_kappa([], []) is None
    assert cohens_kappa(["a", "a"], ["a", "a"]) is None
