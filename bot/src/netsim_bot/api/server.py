"""``grok-bot``: serve the NOC room and the office (uvicorn), and the approvals page for the operator's phone.

grok-bot                 # the office on 0.0.0.0:8095 (BOT_HOST / BOT_PORT), the phone's page on :8097 (BOT_PHONE_PORT)

One process, one engine, two listeners: :class:`netsim_bot.api.phone.Listeners` sends a connection to the office's
app or to the phone's by the port it came in on. ``BOT_PHONE_PORT=0`` turns the phone's listener off.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys


def bind(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.set_inheritable(True)
    return sock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grok-bot", description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=os.environ.get("BOT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BOT_PORT", "8095")))
    parser.add_argument("--phone-port", type=int, default=int(os.environ.get("BOT_PHONE_PORT", "8097")))
    args = parser.parse_args(argv)
    if args.phone_port == args.port:
        parser.error("the phone's port must differ from the office's")
    logging.basicConfig(
        level=os.environ.get("BOT_LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(name)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    import uvicorn

    from netsim_bot.api.app import create_app
    from netsim_bot.api.phone import Listeners, create_phone_app

    office = create_app()
    phone = create_phone_app(lambda: getattr(office.state, "engine", None))
    app = Listeners(office, phone, args.phone_port)
    sockets = [bind(args.host, args.port)]
    if args.phone_port:
        sockets.append(bind(args.host, args.phone_port))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    server.run(sockets=sockets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
