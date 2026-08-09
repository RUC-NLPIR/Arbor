from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from commissioning.cache_campaign.evaluate import (  # noqa: E402
    evaluate_portfolio,
    evaluate_r0,
)
from commissioning.cache_campaign.records import canonical_bytes  # noqa: E402


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        print("error: invalid command line", file=sys.stderr)
        raise SystemExit(2)


def _error(error: object) -> None:
    del error
    print("error: evaluation failed", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(add_help=True)
    parser.add_argument("--rung", choices=("r0", "r1", "r2", "r3"), required=True)
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--task-manifest", type=Path)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--candidate")
    parser.add_argument("--base")
    parser.add_argument("--policy")
    parser.add_argument("--source-receipt", type=Path)
    parser.add_argument("--r0-receipt", type=Path)
    parser.add_argument("--output", type=Path)
    try:
        args = parser.parse_args(argv)
        common = (
            args.checkout,
            args.candidate,
            args.policy,
            args.source_receipt,
            args.output,
        )
        if any(value is None for value in common):
            parser.error("missing required arguments")
        if args.rung == "r3":
            parser.error("R3 is not supported by this evaluator")
        if args.rung == "r0":
            if (
                args.base is None
                or args.task_root is not None
                or args.task_manifest is not None
                or args.r0_receipt is not None
            ):
                parser.error("invalid R0 arguments")
        elif (
            args.base is not None
            or args.task_root is None
            or args.task_manifest is None
            or args.r0_receipt is None
        ):
            parser.error("invalid portfolio arguments")
        assert args.checkout is not None
        assert args.source_receipt is not None
        assert args.output is not None
        checkout = args.checkout.resolve(strict=True)
        source_receipt = args.source_receipt.resolve(strict=True)
        output = args.output.absolute()
        if os.path.lexists(output):
            raise ValueError("output must not exist")
        if args.rung == "r0":
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
        else:
            assert args.task_root is not None
            assert args.task_manifest is not None
            assert args.r0_receipt is not None
            receipt = evaluate_portfolio(
                rung=args.rung,
                task_root=args.task_root.resolve(strict=True),
                task_manifest=args.task_manifest.resolve(strict=True),
                checkout=checkout,
                candidate=args.candidate,
                policy=args.policy,
                source_receipt=source_receipt,
                r0_receipt=args.r0_receipt.resolve(strict=True),
                output=output,
            )
            measurements = receipt.get("measurements")
            failures = receipt.get("failures")
            if not isinstance(measurements, list) or not isinstance(failures, list):
                raise ValueError("invalid portfolio receipt")
            result = {
                "rung": args.rung,
                "receipt_path": str(output / "receipt.json"),
                "receipt_sha256": receipt["receipt_sha256"],
                "measurement_count": len(measurements),
                "failure_count": len(failures),
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
