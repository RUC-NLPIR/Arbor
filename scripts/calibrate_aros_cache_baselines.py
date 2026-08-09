from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from commissioning.cache_campaign.calibrate import calibrate  # noqa: E402
from commissioning.cache_campaign.records import canonical_bytes  # noqa: E402


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        print("error: invalid command line", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(add_help=True)
    parser.add_argument(
        "--task-manifest", type=Path, action="append", required=True
    )
    parser.add_argument("--r0-receipt", type=Path, action="append", required=True)
    parser.add_argument("--receipt", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, action="append", required=True)
    try:
        args = parser.parse_args(argv)
        if (
            len(args.task_manifest) != 1
            or len(args.r0_receipt) != 6
            or len(args.receipt) != 14
            or len(args.output) != 1
        ):
            parser.error("invalid receipt counts")
        output = args.output[0].absolute()
        frozen = calibrate(
            args.task_manifest[0],
            args.r0_receipt,
            args.receipt,
            output,
        )
        result = {
            "calibration_path": str(output.resolve(strict=True)),
            "calibration_sha256": frozen["calibration_sha256"],
        }
        sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
        return 0
    except SystemExit as error:
        return int(error.code)
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        print("error: calibration failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
