from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from commissioning.cache_campaign.records import canonical_bytes  # noqa: E402
from commissioning.cache_campaign.seal import run_r3  # noqa: E402


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        print("error: invalid command line", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(add_help=True)
    parser.add_argument("--frozen-package", required=True, type=Path)
    parser.add_argument("--host-r3-manifest", required=True, type=Path)
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--calibration-sha256", required=True)
    parser.add_argument("--source-receipt", required=True, type=Path)
    parser.add_argument("--candidate-r0-receipt", required=True, type=Path)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        receipt = run_r3(
            args.frozen_package,
            args.host_r3_manifest,
            args.calibration,
            args.calibration_sha256,
            args.source_receipt,
            args.candidate_r0_receipt,
            args.checkout,
            args.ledger,
            args.output,
        )
        result = {
            "state": receipt["state"],
            "receipt_path": receipt["final_receipt_path"],
            "receipt_sha256": receipt["receipt_sha256"],
        }
        sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
        return 0
    except SystemExit as error:
        return int(error.code)
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        print("error: R3 execution failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
