# Contributing

Thanks for taking a look. keycard is early — architectural discussion is as welcome as code.

## Before you start

Open an issue first for anything non-trivial. The scope is deliberately narrow (see the Roadmap in the README), and it'd be a shame for you to build something I then have to turn down. Small fixes and docs improvements can go straight to a PR.

## Development setup

```bash
git clone https://github.com/SamTruss/keycard
cd keycard
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

You'll need Python 3.11+ and a running Docker or Podman daemon.

```bash
pytest              # tests
ruff check .        # lint
ruff format .       # format
mypy src/           # types
```

## Design principles

These are the constraints the project is built around. PRs that cut against them are likely to be declined, so they're worth knowing up front.

**Zero config must keep working.** Every feature needs a sensible default. If using keycard requires editing a file before first connect, the core value proposition is gone.

**No custom client.** The user's existing `ssh` is the entire client story. Nothing that requires installing something on the connecting machine.

**The backend is an interface.** v2 adds Firecracker without touching the SSH layer. Don't reach into Docker specifics from the session handler.

**Scope stays narrow.** Port forwarding, SFTP, persistent volumes, multi-user accounts and a web UI are all out. Each is defensible on its own; together they'd turn keycard into a platform, and platforms need configuring.

**Security claims stay honest.** Don't describe v1 as isolation or sandboxing in a security sense. See [SECURITY.md](SECURITY.md).

## Pull requests

- One logical change per PR
- Tests for new behaviour
- Update the README if you change user-facing behaviour
- Conventional-ish commit messages are appreciated but not enforced

## Reporting bugs

Include your OS, Python version, container backend and version, your config file (redact keys), and the actual vs expected behaviour.

For security issues, don't open an issue — see [SECURITY.md](SECURITY.md).
