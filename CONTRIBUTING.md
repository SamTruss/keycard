# Contributing

Thanks for taking a look. keycard is early — architectural discussion is as welcome as code.

Read [ARCHITECTURE.md](ARCHITECTURE.md) first if you're about to touch `src/` — it maps the codebase and, more importantly, explains the five different ways a room gets torn down and why that isn't as fragile as it sounds.

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

## Releasing

Versioning follows [Semantic Versioning](https://semver.org/): while keycard
is pre-1.0, expect breaking changes on a minor bump. Every notable change
belongs in [CHANGELOG.md](CHANGELOG.md) under `## [Unreleased]` as it lands,
not saved up for release day.

To cut a release:

1. In `CHANGELOG.md`, retitle `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
   and add a fresh empty `## [Unreleased]` above it.
2. Bump `version` in `pyproject.toml` and `__version__` in
   `src/keycard/__init__.py` to match.
3. Commit those two changes, then tag and push:
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
4. Pushing the tag runs [release.yml](.github/workflows/release.yml): it
   verifies the tag matches `pyproject.toml`, builds the sdist and wheel
   once, then fans out to two jobs that both consume that same build —
   `github-release` creates a GitHub Release using that version's
   changelog section as the release notes, and `pypi-publish` publishes to
   PyPI via [trusted publishing](https://docs.pypi.org/trusted-publishers/)
   (no stored token).
5. `pypi-publish` runs under the `pypi` environment, which has a
   required-reviewer rule — the workflow pauses there until someone with
   access approves it in the Actions tab. Nothing reaches PyPI until that
   happens, on purpose: a published version can be yanked but never
   deleted, so this is the one step worth a manual pause.

## Pull requests

- One logical change per PR
- Tests for new behaviour
- Update the README if you change user-facing behaviour
- Update `man/keycard.1` if you change a CLI flag or command
- Add an entry under `## [Unreleased]` in CHANGELOG.md for anything notable
- Conventional-ish commit messages are appreciated but not enforced

## Reporting bugs

Include your OS, Python version, container backend and version, your config file (redact keys), and the actual vs expected behaviour.

For security issues, don't open an issue — see [SECURITY.md](SECURITY.md).
