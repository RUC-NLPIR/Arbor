from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    if Path("candidate-mode.txt").read_text(encoding="utf-8") != "success\n":
        raise SystemExit("candidate-mode.txt is not the expected success value")
    print(json.dumps({"schema_version": 1, "metric": 1.0, "sample_count": 1}))


if __name__ == "__main__":
    main()
