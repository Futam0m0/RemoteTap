"""Interactive command-line client for the RemoteTap service.

Usage::

    python -m remote_tap.client --host 127.0.0.1 --port 8765

Then type ``press``, ``renew``, ``stop``, ``status``, ``help`` or ``quit``.

The client keeps one TCP connection open and sends exactly one command per
input, printing both the JSON it sends and the JSON it receives.

A press is a lease: the device releases it if it is not renewed. After a
successful ``press`` this client prints the renewal interval the device asks
for, and ``renew`` sends one ``PRESS_RENEW`` for the current session.

Every command carries a fresh ``command_id``, so each ``renew`` is a new
command and extends the lease exactly once. Re-sending an identical renewal is
treated as a retry and does **not** extend the lease again. If a renewal is
answered with ``LEASE_EXPIRED`` or ``INVALID_SESSION`` the press is over and the
client should stop renewing; a cached reply never claims an expired press is
still active.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from typing import Any

from .config import DEFAULT_HOST, DEFAULT_PORT
from .protocol.messages import CommandType, encode_line

logger = logging.getLogger("remote_tap.client")

PROMPT = "remotetap> "
HELP_TEXT = """commands:
  press   send PRESS_START with a new session id
  renew   send PRESS_RENEW for the current session id (keeps the press alive)
  stop    send PRESS_STOP for the current session id
  status  show the last state reported by the server (local, no request sent)
  help    show this help
  quit    close the connection and exit

lease notes:
  Every command gets a fresh command_id, and every periodic 'renew' must be a
  NEW command: re-sending the exact same renewal is treated as a retry, is
  answered from the device's cache, and does NOT extend the lease a second time.
  A lost renewal reply does not necessarily mean the renewal failed - the
  'renew' here retries the outcome for you. If a renewal is answered with
  LEASE_EXPIRED or INVALID_SESSION the press is over: stop renewing and treat
  the hold as ended. A cached reply never claims an expired press is still
  active.
"""


class RemoteTapClient:
    """A minimal line-based client for the command channel."""

    def __init__(self, host: str, port: int, connect_timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._session_id: str | None = None
        self._last_state: str | None = None
        self._renew_interval_ms: int | None = None
        self._max_press_ms: int | None = None

    @property
    def session_id(self) -> str | None:
        """Session id currently used by this client."""
        return self._session_id

    @property
    def last_state(self) -> str | None:
        """State most recently reported by the server."""
        return self._last_state

    @property
    def lease_policy(self) -> tuple[int | None, int | None]:
        """The ``(renew_interval_ms, max_press_ms)`` the device last advertised."""
        return self._renew_interval_ms, self._max_press_ms

    def start_new_session(self) -> str:
        """Begin a new session and return its id."""
        self._session_id = uuid.uuid4().hex
        return self._session_id

    async def connect(self) -> None:
        """Open the TCP connection to the server."""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), self._connect_timeout
        )

    async def close(self) -> None:
        """Close the connection cleanly."""
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
        self._reader = None
        self._writer = None

    async def send(self, command_type: CommandType, session_id: str) -> dict[str, Any]:
        """Send one command and return the decoded response.

        Each command gets its own ``command_id``; the server echoes it in the
        acknowledgement so requests and responses can be matched.
        """
        if self._reader is None or self._writer is None:
            raise RuntimeError("connect() must be awaited first")

        command = {
            "type": command_type.value,
            "command_id": uuid.uuid4().hex,
            "session_id": session_id,
        }
        print(f"[client] -> {json.dumps(command)}")
        self._writer.write(encode_line(command))
        await self._writer.drain()

        line = await self._reader.readuntil(b"\n")
        response: dict[str, Any] = json.loads(line.decode("utf-8"))
        print(f"[client] <- {json.dumps(response)}")

        state = response.get("state")
        if isinstance(state, str):
            self._last_state = state
        # The device advertises its lease policy on a press or a renewal, so the
        # client never has to guess the timings it is being measured against.
        interval = response.get("renew_interval_ms")
        if isinstance(interval, int):
            self._renew_interval_ms = interval
        max_press = response.get("max_press_ms")
        if isinstance(max_press, int):
            self._max_press_ms = max_press
        return response


async def run_interactive(client: RemoteTapClient) -> None:
    """Read commands from the terminal until the user quits.

    ``input()`` blocks the event loop, which is acceptable here: this client
    talks to exactly one server and has nothing else to do meanwhile.
    """
    print(HELP_TEXT)

    while True:
        try:
            raw = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not raw:
            continue

        command = raw.lower()
        if command in {"quit", "exit", "q"}:
            return
        if command == "help":
            print(HELP_TEXT)
            continue
        if command == "status":
            print(
                f"[client] last state={client.last_state or 'unknown'} "
                f"session={client.session_id or 'none'}"
            )
            continue
        if command == "press":
            client.start_new_session()
            if not await _send_or_report(client, CommandType.PRESS_START):
                return
            _report_lease_policy(client)
            continue
        if command == "renew":
            if client.session_id is None:
                print("[client] no session yet: send 'press' first")
                continue
            if not await _send_or_report(client, CommandType.PRESS_RENEW):
                return
            continue
        if command == "stop":
            if client.session_id is None:
                print("[client] no session yet: send 'press' first")
                continue
            if not await _send_or_report(client, CommandType.PRESS_STOP):
                return
            continue

        print(f"[client] unknown input {raw!r} - try 'help'")


async def _send_or_report(client: RemoteTapClient, command_type: CommandType) -> bool:
    """Send one command, reporting transport failures instead of crashing.

    Returns:
        ``False`` if the connection is no longer usable.
    """
    try:
        await client.send(command_type, client.session_id or "")
    except asyncio.IncompleteReadError:
        print("[client] the server closed the connection")
        return False
    except OSError as exc:
        print(f"[client] connection error: {exc}")
        return False
    except json.JSONDecodeError as exc:
        print(f"[client] could not decode the server response: {exc}")
        return False
    return True


def _report_lease_policy(client: RemoteTapClient) -> None:
    """Print how often the device wants a press to be renewed.

    The numbers come from the device's own acknowledgement, so they stay right
    when the service is started with different ``--*-ms`` limits.
    """
    interval, max_press = client.lease_policy
    if interval is None:
        return
    print(
        f"[client] the device releases this press unless 'renew' is sent every "
        f"{interval} ms, and at {max_press} ms at the latest"
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the client."""
    parser = argparse.ArgumentParser(
        prog="remote_tap.client",
        description="Interactive RemoteTap client (Phase 2: TCP + press lease).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="server address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="server port (default: %(default)s)")
    parser.add_argument("--verbose", action="store_true", help="log at DEBUG level")
    return parser


async def _main_async(host: str, port: int) -> None:
    """Connect, run the interactive loop, and always disconnect cleanly."""
    client = RemoteTapClient(host, port)
    print(f"[client] connecting to {host}:{port} ...")
    await client.connect()
    print("[client] connected")
    try:
        await run_interactive(client)
    finally:
        await client.close()
        print("[client] disconnected")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the interactive client."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        asyncio.run(_main_async(args.host, args.port))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print()
    except (ConnectionRefusedError, OSError) as exc:
        print(f"[client] could not connect to {args.host}:{args.port}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
