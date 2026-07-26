"""eval.py — PROTECTED evaluation harness (toy benchmark fixture).

Prints exactly one line ``score: <float>`` for ``--split dev|test``. This is
the contract arbor's eval_run parses. dev/test are identical here (the objective
is deterministic), satisfying the disjoint-split requirement trivially.
"""
from __future__ import annotations

import argparse

import solution


def evaluate(split: str) -> float:
    return float(solution.solve())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--split", choices=["dev", "test"], default="dev")
    print(f"score: {evaluate(p.parse_args().split):.4f}")


if __name__ == "__main__":
    main()
