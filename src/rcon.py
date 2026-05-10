"""Small Rust WebRCON client used by the updater."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote

from dotenv import load_dotenv
from websocket import WebSocketException, WebSocketTimeoutException, create_connection

LOGGER = logging.getLogger(__name__)


class RconError(RuntimeError):
    """Raised when a WebRCON command cannot be delivered."""


@dataclass(frozen=True)
class RconConfig:
    """Connection settings for Rust WebRCON."""

    host: str = "rust-server"
    port: int = 28016
    password: str = ""
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "RconConfig":
        """Build RCON config from environment variables."""

        load_dotenv()
        password = os.getenv("RCON_PASSWORD") or os.getenv("PASSWORD") or ""
        port_raw = os.getenv("RCON_PORT", "28016")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise RconError("RCON_PORT must be an integer") from exc

        timeout_raw = os.getenv("RCON_TIMEOUT_SECONDS", "10")
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError as exc:
            raise RconError("RCON_TIMEOUT_SECONDS must be a number") from exc

        return cls(
            host=os.getenv("RCON_HOST", "rust-server"),
            port=port,
            password=password,
            timeout_seconds=timeout_seconds,
        )

    def websocket_url(self) -> str:
        """Return the WebRCON URL without exposing it in logs."""

        return f"ws://{self.host}:{self.port}/{quote(self.password, safe='')}"


def send_rcon_command(command: str, config: RconConfig | None = None) -> None:
    """Send one command to Rust WebRCON.

    Rust WebRCON authenticates with the password in the websocket path. There is
    no username in this protocol.
    """

    rcon_config = config or RconConfig.from_env()
    if not rcon_config.password:
        raise RconError("RCON password is missing")

    payload = {
        "Identifier": _new_identifier(),
        "Message": command,
        "Name": "rust-server-automation",
    }

    websocket = None
    try:
        websocket = create_connection(
            rcon_config.websocket_url(),
            timeout=rcon_config.timeout_seconds,
        )
        websocket.send(json.dumps(payload))
        websocket.settimeout(min(2.0, rcon_config.timeout_seconds))
        _read_matching_response(websocket, payload["Identifier"])
    except WebSocketTimeoutException as exc:
        raise RconError("Timed out while communicating with WebRCON") from exc
    except WebSocketException as exc:
        raise RconError("Failed to connect to or send command over WebRCON") from exc
    except OSError as exc:
        raise RconError("Failed to reach WebRCON endpoint") from exc
    finally:
        if websocket is not None:
            try:
                websocket.close()
            except WebSocketException:
                LOGGER.debug("Failed to close WebRCON websocket cleanly")


def _read_matching_response(websocket, identifier: int) -> None:
    """Read briefly for a matching response, if Rust sends one."""

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            raw_message = websocket.recv()
        except WebSocketTimeoutException:
            return

        if not raw_message:
            raise RconError("WebRCON connection closed before command response")

        try:
            response = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            raise RconError("Received non-JSON WebRCON response") from exc

        if response.get("Identifier") == identifier:
            return

        LOGGER.debug(
            "Ignoring unrelated WebRCON frame with identifier %s",
            response.get("Identifier"),
        )


def _new_identifier() -> int:
    """Return a Rust WebRCON-safe identifier.

    Rust deserializes Identifier as a signed Int32, so Unix millisecond
    timestamps are too large.
    """

    return random.randint(1, 2_147_483_647)


def main(argv: list[str] | None = None) -> int:
    """Send WebRCON commands from the command line."""

    parser = argparse.ArgumentParser(description="Send a Rust WebRCON command")
    parser.add_argument("commands", nargs="+", help="Command(s) to send")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = RconConfig.from_env()
        for command in args.commands:
            LOGGER.info("Sending RCON command: %s", command)
            send_rcon_command(command, config=config)
    except RconError as exc:
        LOGGER.error("%s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
