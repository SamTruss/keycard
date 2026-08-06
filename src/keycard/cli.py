"""Command-line entry point for keycard."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from . import __version__
from .config import load as load_config


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Disposable SSH sandboxes. One command, zero setup."""


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to keycard.toml.",
)
@click.option("--host", default=None, help="Address to bind. Overrides config.")
@click.option("--port", default=None, type=int, help="Port to listen on. Overrides config.")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def up(
    config_path: Path | None,
    host: str | None,
    port: int | None,
    verbose: bool,
) -> None:
    """Run the keycard server."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  keycard: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(config_path)
    if host is not None:
        cfg.listen = f"{host}:{cfg.port}"
    if port is not None:
        cfg.listen = f"{cfg.host}:{port}"

    from .server import serve

    try:
        asyncio.run(serve(cfg))
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        click.echo("\nkeycard: front desk closed")


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to keycard.toml.",
)
def rooms(config_path: Path | None) -> None:
    """List available rooms."""
    cfg = load_config(config_path)
    if not cfg.rooms:
        click.echo("No rooms configured.")
        return

    max_name = max(len(r.name) for r in cfg.rooms.values())
    for room in cfg.rooms.values():
        default = " (default)" if room.name == cfg.default_room else ""
        extras = []
        if room.memory:
            extras.append(f"mem={room.memory}")
        if room.cpus:
            extras.append(f"cpus={room.cpus}")
        if room.network == "none":
            extras.append("offline")
        extra_str = f"  [{', '.join(extras)}]" if extras else ""
        click.echo(f"  {room.name:<{max_name}}  {room.image}{default}{extra_str}")
