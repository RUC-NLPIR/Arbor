"""solution.py — the edit surface (toy benchmark fixture for docker/test_mcp.py).

The score is a deterministic parabola of LEARNING_RATE peaking at 0.1:
  LR=0.01 -> 19   (baseline)
  LR=0.1  -> 100  (peak; the "good" hypothesis)
  LR=1.0  -> -8000 (overshoot; the "bad" hypothesis to prune)
"""
from __future__ import annotations

LEARNING_RATE = 0.01  # baseline


def solve() -> float:
    return round(-(LEARNING_RATE - 0.1) ** 2 * 10000 + 100, 4)
