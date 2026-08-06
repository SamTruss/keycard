"""Command-line entry point for keycard."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from . import __version__
from .server import AUTHORIZED_KEYS, HOST_KEY, serve


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Disposable SSH sandboxes. One command, zero setup."""


@main.command()
@click.option("--host", default="", help="Address to bind. Default: all interfaces.")
@click.option("--port", default=2222, show_default=True, help="Port to listen on.")
@click.option(
    "--image",
    default="ubuntu:24.04",
    show_default=True,
    help="Image every room is built from. v1 serves one image for all usernames.",
)
@click.option(
    "--authorized-keys",
    type=click.Path(path_type=Path),
    default=AUTHORIZED_KEYS,
    show_default=True,
    help="Public keys permitted to check in.",
)
@click.option(
    "--host-key",
    type=click.Path(path_type=Path),
    default=HOST_KEY,
    show_default=True,
    help="Server host key. Generated on first run.",
)
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def up(
    host: str,
    port: int,
    image: str,
    authorized_keys: Path,
    host_key: Path,
    verbose: bool,
) -> None:
    """Run the keycard server."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  keycard: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(
            serve(
                host=host,
                port=port,
                image=image,
                authorized_keys=authorized_keys,
                host_key=host_key,
            )
        )
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        click.echo("\nkeycard: front desk closed")
