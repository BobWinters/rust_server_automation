# Rust Server Automation

This project builds a small Dockerized updater for my Rust dedicated server. It monitors `bobwinters/rust-game-server:latest-oxide`, compares it with the image currently used by the running `rust-server` container, and performs a graceful update when a newer image is available.

When an update is found, the container warns players over Rust WebRCON, announces every minute during the default 15-minute countdown, sends `server.save`, and recreates the `rust-server` service with Docker Compose.

## Why This Exists

I build my own Rust/Oxide image and want my server to update from that image without waiting for another maintainer to publish theirs. Updates should happen automatically, but not abruptly: players get warning time, the server saves, and the existing persistent data stays outside the image/container layer.

This is not a host cron script, Watchtower setup, Kubernetes deployment, Portainer workflow, or systemd timer.

## Build Locally

```bash
docker build -t bobwinters/rust-server-automation:latest .
```

## Docker Compose Service

### Separate Updater Compose File

This repository includes `compose.updater.yaml`, which runs the updater without editing the live Rust server Compose file. It builds the updater locally as `rust-server-automation:local`, joins the existing `rust_default` Docker network, and mounts the private live Rust Compose directory at `/compose`.

By default, the separate updater compose file expects the private live server config to be in a sibling directory:

```text
../rust_server_live/compose.yaml
../rust_server_live/.env
```

You can override that path without editing this repo:

```bash
LIVE_RUST_COMPOSE_DIR=/path/to/private/rust_server_live docker compose -f compose.updater.yaml up -d --build rust-updater
```

Start or recreate the updater:

```bash
docker compose -f compose.updater.yaml up -d --build rust-updater
docker logs -f rust-updater
```

The separate compose file currently uses:

```yaml
      COMPOSE_FILE: "/compose/compose.yaml"
      DRY_RUN: "false"
```

Set `DRY_RUN` to `"true"` before starting the updater if you want to verify detection without sending countdown RCON messages, saving, restarting, or pruning.

After changing `DRY_RUN`, recreate the updater:

```bash
docker compose -f compose.updater.yaml up -d --build rust-updater
```

### Live Rust Compose Block

If you prefer to place the updater directly in your live Rust Compose file, add this service block to that private Compose file:

```yaml
  rust-updater:
    image: "bobwinters/rust-server-automation:latest"
    container_name: "rust-updater"
    restart: unless-stopped
    depends_on:
      - rust-server
    env_file: .env
    environment:
      RUST_SERVICE_NAME: "rust-server"
      RUST_CONTAINER_NAME: "rust-server"
      RUST_IMAGE: "bobwinters/rust-game-server:latest-oxide"
      COMPOSE_PROJECT_DIR: "/compose"
      COMPOSE_FILE: "/compose/docker-compose.yml"
      CHECK_INTERVAL_SECONDS: "900"
      UPDATE_COUNTDOWN_SECONDS: "900"
      RCON_HOST: "rust-server"
      RCON_PORT: "${RCON_PORT}"
      RCON_PASSWORD: "${PASSWORD}"
      FORCE_UPDATE_WITHOUT_RCON: "false"
      PRUNE_OLD_IMAGES: "true"
      DRY_RUN: "false"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /path/to/private/rust_server_live:/compose
    networks:
      - default
```

The updater should run on the same Compose network as `rust-server`. `RCON_HOST=rust-server` resolves over Docker networking, even if RCON is also published externally for local administration.

The default examples use `/compose/docker-compose.yml` as the generic Compose file path. If your private live Compose file is named `compose.yaml`, set `COMPOSE_FILE: "/compose/compose.yaml"` in the updater service or create a `docker-compose.yml` symlink.

Start it from the existing Compose directory:

```bash
docker compose up -d rust-updater
```

## Configuration

The updater reads configuration from environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `RUST_SERVICE_NAME` | `rust-server` | Docker Compose service to recreate. |
| `RUST_CONTAINER_NAME` | `rust-server` | Running container to inspect for the current image ID. |
| `RUST_IMAGE` | `bobwinters/rust-game-server:latest-oxide` | Image tag to pull and compare. |
| `COMPOSE_PROJECT_DIR` | `/compose` | Mounted Compose project directory. |
| `COMPOSE_FILE` | `/compose/docker-compose.yml` | Mounted Compose file path. |
| `CHECK_INTERVAL_SECONDS` | `900` | Seconds between update checks. |
| `UPDATE_COUNTDOWN_SECONDS` | `900` | Graceful restart countdown. |
| `RCON_HOST` | `rust-server` | WebRCON hostname. Use `127.0.0.1` for local host testing. |
| `RCON_PORT` | `28016` | WebRCON port. |
| `RCON_PASSWORD` | empty | Preferred WebRCON password variable. |
| `PASSWORD` | empty | Fallback password variable for compatibility with the existing `.env`. |
| `RCON_WEB` | `1` | Must be `1`; this updater uses WebRCON. |
| `FORCE_UPDATE_WITHOUT_RCON` | `false` | If `true`, restart even when RCON notification/save fails. |
| `PRUNE_OLD_IMAGES` | `true` | Run `docker image prune -f` after updating. |
| `DRY_RUN` | `false` | Log actions without restarting or pruning. |
| `SEND_RCON_IN_DRY_RUN` | `false` | Allow real RCON messages during dry runs. |

`RCON_PASSWORD` is preferred. If it is empty or unset, the updater falls back to `PASSWORD`. If both are missing, or if the resolved password is the placeholder `[password]`, startup validation fails. The password is never printed in logs.

## Update Flow

On every check, the updater:

1. Inspects the running `rust-server` container.
2. Reads the image ID used by that running container.
3. Pulls `bobwinters/rust-game-server:latest-oxide`.
4. Inspects the pulled local image ID.
5. Logs `No update available` if the IDs match.
6. If they differ, announces the countdown over WebRCON.
7. Sends `server.save`.
8. Runs:

```bash
docker compose --project-directory /compose -f /compose/docker-compose.yml up -d rust-server
```

9. Optionally runs:

```bash
docker image prune -f
```

The updater never runs `docker compose down` and never runs `docker compose down -v`.

## Countdown

The default countdown is 900 seconds. With that default, messages are sent at 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, and 1 minute remaining, followed by 30 seconds, 10 seconds, then `server.save`.

If `UPDATE_COUNTDOWN_SECONDS` is changed, the updater announces every full minute remaining, sends a 30-second warning when the countdown is at least 60 seconds, sends a 10-second warning, then saves.

## RCON Security

Rust WebRCON uses a password in the websocket URL. It does not use a username. Use a long random password, do not commit it, and do not expose RCON publicly unless required.

For local testing:

```bash
RCON_HOST=127.0.0.1 RCON_PORT=28016 RCON_PASSWORD='your-password' python -m src.rcon 'global.say "RCON test"'
RCON_HOST=127.0.0.1 RCON_PORT=28016 RCON_PASSWORD='your-password' python -m src.rcon 'server.save'
```

## Docker Socket Security

The updater mounts `/var/run/docker.sock`, which gives the container powerful control over Docker on the host. Only run trusted images with this mount. This automation image should be built from this repository, reviewed, and treated as privileged infrastructure.

The Compose directory is mounted at `/compose` so the updater can run Docker Compose against the existing Rust service definition.

## Wipe Safety

Restarting or recreating the container does not wipe the server by itself. Rust server persistence must stay outside the image/container layer.

Important safety rules:

- Do not delete `/opt/rust`.
- Do not delete Docker volumes.
- Do not run `docker compose down -v`.
- Do not change `SERVER_NAME` or `server.identity` unless intentionally creating a new server identity.
- Do not change `SERVER_SEED`, `SERVER_WORLDSIZE`, or map level unless intentionally changing the map.
- Facepunch force wipes can still happen.

The updater only recreates the `rust-server` service and does not change server identity, map seed, world size, mounted data paths, or volumes.

## Dry Run

Enable dry run mode to verify detection and planned actions without restarting the server or pruning images:

```yaml
      DRY_RUN: "true"
```

By default, dry runs also skip real RCON messages and log what would have been sent. To test real RCON delivery during a dry run:

```yaml
      DRY_RUN: "true"
      SEND_RCON_IN_DRY_RUN: "true"
```

## Troubleshooting

Useful commands:

```bash
docker logs rust-updater
docker logs rust-server
docker compose pull rust-server
docker image inspect bobwinters/rust-game-server:latest-oxide
docker inspect rust-server
```

Verify the updater can see Docker from inside its container:

```bash
docker exec rust-updater docker ps
docker exec rust-updater docker inspect rust-server
docker exec rust-updater docker compose --project-directory /compose -f /compose/docker-compose.yml version
```
