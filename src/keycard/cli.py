"""Command-line entry point for keycard."""

import click

from . import __version__


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Disposable SSH sandboxes. One command, zero setup."""


@main.command()
@click.option("--config", type=click.Path(), help="Path to keycard.toml.")
@click.option("--listen", default=":2222", help="Address and port to listen on.")
def serve(config: str | None, listen: str) -> None:
    """Run the keycard SSH server."""
    raise NotImplementedError("Not yet implemented — see the roadmap in README.md")


@main.command()
def rooms() -> None:
    """List configured rooms."""
    raise NotImplementedError("Not yet implemented — see the roadmap in README.md")
