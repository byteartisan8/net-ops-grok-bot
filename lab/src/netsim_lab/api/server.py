"""``lab-api``: serve the API (uvicorn), or export / check ``lab/openapi.json``.

lab-api                                  # 0.0.0.0:8090 (LAB_API_HOST / LAB_API_PORT)
lab-api --openapi lab/openapi.json       # write the OpenAPI document
lab-api --openapi lab/openapi.json --check
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def openapi_text() -> str:
    from netsim_lab.api.app import create_app

    app = create_app(state=None)
    return json.dumps(app.openapi(), indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab-api", description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=os.environ.get("LAB_API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LAB_API_PORT", "8090")))
    parser.add_argument("--openapi", type=Path, help="write the OpenAPI document to this file and exit")
    parser.add_argument("--check", action="store_true", help="with --openapi: fail if the file is not current")
    args = parser.parse_args(argv)
    if args.openapi:
        text = openapi_text()
        if args.check:
            current = args.openapi.read_text(encoding="utf-8") if args.openapi.exists() else ""
            if current != text:
                print(f"lab-api: {args.openapi} is not current (run `make lab-openapi`)", file=sys.stderr)
                return 1
            print(f"OK: {args.openapi} is current")
            return 0
        args.openapi.write_text(text, encoding="utf-8")
        print(f"lab-api: wrote {args.openapi}")
        return 0
    import uvicorn

    from netsim_lab.api.app import create_app

    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        log_level=os.environ.get("LAB_API_LOG_LEVEL", "info"),
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
