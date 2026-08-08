from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from commissioning.cache_campaign.evaluate import evaluate_r0  # noqa: E402
from commissioning.cache_campaign.records import canonical_bytes  # noqa: E402


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(f"error: {' '.join(message.split())}", file=sys.stderr)
        raise SystemExit(2)


def _error(error: object) -> None:
    message = " ".join(str(error).split()) or error.__class__.__name__
    print(f"error: {message[:500]}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(add_help=True)
    parser.add_argument("--rung", choices=("r0",), required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        checkout = args.checkout.resolve(strict=True)
        source_receipt = args.source_receipt.resolve(strict=True)
        output = args.output.absolute()
        if os.path.lexists(output):
            raise ValueError("output must not exist")
        receipt = evaluate_r0(
            checkout=checkout,
            base=args.base,
            candidate=args.candidate,
            policy=args.policy,
            source_receipt=source_receipt,
            output=output,
        )
        result = {
            "rung": "r0",
            "receipt_path": str(output / "receipt.json"),
            "receipt_sha256": receipt["receipt_sha256"],
            "checks": receipt["checks"],
        }
        sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
        return 0
    except SystemExit as error:
        return int(error.code)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        _error(error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
