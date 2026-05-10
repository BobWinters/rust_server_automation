"""Dockerized updater for a Rust dedicated server container."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound
from dotenv import load_dotenv

from src.rcon import RconConfig, RconError, send_rcon_command

LOGGER = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when startup configuration is invalid."""


class UpdateAborted(RuntimeError):
    """Raised when a graceful update cannot safely continue."""


@dataclass(frozen=True)
class UpdaterConfig:
    """Environment-driven updater configuration."""

    rust_service_name: str
    rust_container_name: str
    rust_image: str
    compose_project_dir: str
    compose_file: str
    check_interval_seconds: int
    update_countdown_seconds: int
    rcon_host: str
    rcon_port: int
    rcon_password: str
    rcon_web: str
    force_update_without_rcon: bool
    prune_old_images: bool
    dry_run: bool
    send_rcon_in_dry_run: bool

    @property
    def rcon_config(self) -> RconConfig:
        return RconConfig(
            host=self.rcon_host,
            port=self.rcon_port,
            password=self.rcon_password,
        )


def main() -> None:
    """Start the updater loop."""

    configure_logging()
    try:
        config = load_config()
        docker_client = docker.from_env()
        validate_startup(config, docker_client)
    except ConfigError as exc:
        LOGGER.error("Startup validation failed: %s", exc)
        sys.exit(1)
    except DockerException as exc:
        LOGGER.error("Docker startup validation failed: %s", exc)
        sys.exit(1)

    LOGGER.info("Rust server updater started")
    LOGGER.info(
        "Monitoring image %s for container %s",
        config.rust_image,
        config.rust_container_name,
    )
    if config.dry_run:
        LOGGER.info("DRY_RUN=true: restart and image prune actions will only be logged")

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    run_loop(config, docker_client, stop_event)


def configure_logging() -> None:
    """Configure timestamped logs suitable for docker logs."""

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_config() -> UpdaterConfig:
    """Load updater configuration from environment."""

    load_dotenv()
    password = os.getenv("RCON_PASSWORD") or os.getenv("PASSWORD") or ""

    return UpdaterConfig(
        rust_service_name=os.getenv("RUST_SERVICE_NAME", "rust-server"),
        rust_container_name=os.getenv("RUST_CONTAINER_NAME", "rust-server"),
        rust_image=os.getenv("RUST_IMAGE", "bobwinters/rust-game-server:latest-oxide"),
        compose_project_dir=os.getenv("COMPOSE_PROJECT_DIR", "/compose"),
        compose_file=os.getenv("COMPOSE_FILE", "/compose/docker-compose.yml"),
        check_interval_seconds=_env_int("CHECK_INTERVAL_SECONDS", 900),
        update_countdown_seconds=_env_int("UPDATE_COUNTDOWN_SECONDS", 900),
        rcon_host=os.getenv("RCON_HOST", "rust-server"),
        rcon_port=_env_int("RCON_PORT", 28016),
        rcon_password=password,
        rcon_web=os.getenv("RCON_WEB", "1"),
        force_update_without_rcon=_env_bool("FORCE_UPDATE_WITHOUT_RCON", False),
        prune_old_images=_env_bool("PRUNE_OLD_IMAGES", True),
        dry_run=_env_bool("DRY_RUN", False),
        send_rcon_in_dry_run=_env_bool("SEND_RCON_IN_DRY_RUN", False),
    )


def validate_startup(config: UpdaterConfig, docker_client) -> None:
    """Validate fatal startup requirements before entering the loop."""

    if not config.rust_container_name:
        raise ConfigError("RUST_CONTAINER_NAME is required")
    if not config.rust_service_name:
        raise ConfigError("RUST_SERVICE_NAME is required")
    if not config.rust_image:
        raise ConfigError("RUST_IMAGE is required")
    if config.rcon_web != "1":
        raise ConfigError("RCON_WEB must be 1 for Rust WebRCON")
    if not config.rcon_password:
        raise ConfigError("RCON_PASSWORD or PASSWORD is required")
    if config.rcon_password == "[password]":
        raise ConfigError('RCON password must not be the placeholder "[password]"')
    if config.check_interval_seconds <= 0:
        raise ConfigError("CHECK_INTERVAL_SECONDS must be greater than zero")
    if config.update_countdown_seconds < 0:
        raise ConfigError("UPDATE_COUNTDOWN_SECONDS must not be negative")
    if not Path(config.compose_file).exists():
        raise ConfigError(f"Compose file does not exist: {config.compose_file}")

    try:
        docker_client.ping()
    except DockerException as exc:
        raise ConfigError("Docker socket is not accessible") from exc

    try:
        _run_command(["docker", "compose", "version"], "validate docker compose")
    except RuntimeError as exc:
        raise ConfigError(str(exc)) from exc


def run_loop(config: UpdaterConfig, docker_client, stop_event: threading.Event) -> None:
    """Run update checks until stopped."""

    while not stop_event.is_set():
        try:
            check_once(config, docker_client)
        except UpdateAborted as exc:
            LOGGER.error("%s", exc)
        except Exception:
            LOGGER.exception("Update check failed; will retry after sleep")

        LOGGER.info(
            "Sleeping for %s seconds before next check",
            config.check_interval_seconds,
        )
        stop_event.wait(config.check_interval_seconds)

    LOGGER.info("Rust server updater stopped")


def check_once(config: UpdaterConfig, docker_client) -> None:
    """Check the Rust server image and update when a newer image is available."""

    current_image_id = get_running_container_image_id(
        docker_client,
        config.rust_container_name,
    )
    LOGGER.info("Current running image ID: %s", _short_image_id(current_image_id))

    LOGGER.info("Pulling latest image: %s", config.rust_image)
    docker_client.images.pull(config.rust_image)
    latest_image = docker_client.images.get(config.rust_image)
    latest_image_id = latest_image.id
    LOGGER.info("Latest local image ID: %s", _short_image_id(latest_image_id))

    if current_image_id == latest_image_id:
        LOGGER.info("No update available")
        return

    LOGGER.info(
        "New update available: running=%s latest=%s",
        _short_image_id(current_image_id),
        _short_image_id(latest_image_id),
    )
    perform_update(config)


def get_running_container_image_id(docker_client, container_name: str) -> str:
    """Return the image ID used by the currently running Rust container."""

    try:
        container = docker_client.containers.get(container_name)
    except NotFound as exc:
        raise RuntimeError(f"Rust container not found: {container_name}") from exc

    container.reload()
    image_id = container.attrs.get("Image") or container.image.id
    if not image_id:
        raise RuntimeError(f"Could not determine image ID for container {container_name}")
    return image_id


def perform_update(config: UpdaterConfig) -> None:
    """Run countdown, save, recreate the Rust server, and optionally prune images."""

    if config.dry_run and not config.send_rcon_in_dry_run:
        LOGGER.info(
            "[dry-run] Would run graceful countdown for %s seconds",
            config.update_countdown_seconds,
        )
        LOGGER.info("[dry-run] Would send server.save")
        LOGGER.info(
            "[dry-run] Would recreate service %s with Docker Compose",
            config.rust_service_name,
        )
        if config.prune_old_images:
            LOGGER.info("[dry-run] Would prune old Docker images")
        return

    try:
        run_countdown(config)
    except RconError as exc:
        if not config.force_update_without_rcon:
            raise UpdateAborted(
                "RCON notification/save failed; aborting update because "
                "FORCE_UPDATE_WITHOUT_RCON=false"
            ) from exc
        LOGGER.warning(
            "RCON failed, but FORCE_UPDATE_WITHOUT_RCON=true; proceeding with "
            "restart without successful RCON notification"
        )

    restart_service(config)
    prune_images(config)


def run_countdown(config: UpdaterConfig) -> None:
    """Send countdown warnings, save the server, and wait for the restart window."""

    remaining = config.update_countdown_seconds
    minute_marks = list(range(remaining // 60, 0, -1))

    for minute in minute_marks:
        mark_seconds = minute * 60
        sleep_seconds = remaining - mark_seconds
        _sleep_for_countdown(sleep_seconds)
        if minute == remaining // 60:
            _send_global_say(
                config,
                f"Server update available. Restarting in {minute} {_minute_word(minute)}.",
            )
        elif minute == 1:
            _send_global_say(
                config,
                "Server restarting in 1 minute for update. Please get safe.",
            )
        else:
            _send_global_say(
                config,
                f"Server restarting in {minute} minutes for update.",
            )
        remaining = mark_seconds

    if config.update_countdown_seconds >= 60:
        _sleep_for_countdown(max(remaining - 30, 0))
        _send_global_say(
            config,
            "Server restarting in 30 seconds for update. Please get safe.",
        )
        remaining = 30

    _sleep_for_countdown(max(remaining - 10, 0))
    _send_global_say(config, "Server restarting in 10 seconds for update.")
    remaining = min(remaining, 10)

    _sleep_for_countdown(remaining)
    _send_rcon(config, "server.save")
    _sleep_for_countdown(5)


def restart_service(config: UpdaterConfig) -> None:
    """Recreate the Rust server service with Docker Compose."""

    command = [
        "docker",
        "compose",
        "--project-directory",
        config.compose_project_dir,
        "-f",
        config.compose_file,
        "up",
        "-d",
        config.rust_service_name,
    ]

    if config.dry_run:
        LOGGER.info("[dry-run] Would run: %s", " ".join(command))
        return

    LOGGER.info("Recreating Rust service with Docker Compose")
    result = _run_command(command, "recreate rust-server")
    if result.stdout:
        LOGGER.info("docker compose stdout: %s", result.stdout.strip())
    if result.stderr:
        LOGGER.info("docker compose stderr: %s", result.stderr.strip())


def prune_images(config: UpdaterConfig) -> None:
    """Prune dangling/unused old Docker images when enabled."""

    if not config.prune_old_images:
        LOGGER.info("PRUNE_OLD_IMAGES=false: skipping image prune")
        return

    command = ["docker", "image", "prune", "-f"]
    if config.dry_run:
        LOGGER.info("[dry-run] Would run: %s", " ".join(command))
        return

    LOGGER.info("Pruning old Docker images")
    result = _run_command(command, "prune old images")
    if result.stdout:
        LOGGER.info("docker image prune stdout: %s", result.stdout.strip())
    if result.stderr:
        LOGGER.info("docker image prune stderr: %s", result.stderr.strip())


def _send_global_say(config: UpdaterConfig, message: str) -> None:
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    _send_rcon(config, f'global.say "{escaped}"')


def _send_rcon(config: UpdaterConfig, command: str) -> None:
    if config.dry_run and not config.send_rcon_in_dry_run:
        LOGGER.info("[dry-run] Would send RCON command: %s", command)
        return

    LOGGER.info("Sending RCON command: %s", command)
    send_rcon_command(command, config=config.rcon_config)


def _sleep_for_countdown(seconds: int) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _run_command(command: list[str], action: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Required command not found while trying to {action}: {command[0]}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout.strip() if exc.stdout else ""
        stderr = exc.stderr.strip() if exc.stderr else ""
        detail = f"stdout={stdout!r} stderr={stderr!r}"
        raise RuntimeError(f"Command failed while trying to {action}: {detail}") from exc


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _short_image_id(image_id: str) -> str:
    if image_id.startswith("sha256:"):
        return image_id[:19]
    return image_id


def _minute_word(minutes: int) -> str:
    return "minute" if minutes == 1 else "minutes"


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def handle_signal(signum, _frame) -> None:
        LOGGER.info("Received signal %s; stopping after current operation", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
