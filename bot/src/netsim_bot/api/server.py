"""``grok-bot``: serve the NOC room and the bot (uvicorn), or check that the Grok key works.

grok-bot                 # 0.0.0.0:8095 (BOT_HOST / BOT_PORT)
grok-bot --check-grok    # one function-calling round trip with the configured key and model, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grok-bot", description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=os.environ.get("BOT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BOT_PORT", "8095")))
    parser.add_argument("--check-grok", action="store_true", help="test the xAI key and model, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("BOT_LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(name)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.check_grok:
        from netsim_bot.engine import Engine

        async def check() -> dict[str, object]:
            eng = Engine()
            try:
                return await eng.grok_check()
            finally:
                await eng.stop()

        result = asyncio.run(check())
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1
    import uvicorn

    from netsim_bot.api.app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
