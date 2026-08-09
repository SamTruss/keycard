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

Two parts of the tree need more than that, and both skip cleanly without it,
so a plain `pytest` is green either way:

- **`guest-agent/`** is Rust — `cargo fmt`, `cargo clippy --all-targets`,
  `cargo test` from inside that directory. Its tests stand vsock in with TCP;
  see ARCHITECTURE.md on why that substitution has already hidden one bug.
- **`tests/test_firecracker_integration.py`** boots real microVMs and needs
  `/dev/kvm`, a `firecracker` binary, a guest kernel and a rootfs built by
  `rootfs/build.sh`. Run it with `-rs` and it names the first thing missing.
  Nothing in CI can run it, so if you change that backend, say in your PR
  whether you booted one.

## Design principles

These are the constraints the project is built around. PRs that cut against them are likely to be declined, so they're worth knowing up front.

**Zero config must keep working.** Every feature needs a sensible default. If using keycard requires editing a file before first connect, the core value proposition is gone.

**No custom client.** The user's existing `ssh` is the entire client story. Nothing that requires installing something on the connecting machine.

**The backend is an interface.** Firecracker was added without `server.py` learning it exists, and the one change the session handler did need was a latent bug a slower backend exposed, not a special case. Don't reach into Docker specifics from the session handler.

**keycard never needs privilege.** Not root, not `sudo`, not added capabilities — on any backend. This is a hard constraint, not a preference: it is why the microVM backend confines its VMM with Landlock instead of Firecracker's jailer, which requires root, and why tap networking is deferred rather than built. A design that needs elevated privileges will be declined even if it works, because SECURITY.md's whole scope rests on this one. If you think you've found the exception, open an issue before writing it.

**Scope stays narrow.** Port forwarding, SFTP, persistent volumes, multi-user accounts and a web UI are all out. Each is defensible on its own; together they'd turn keycard into a platform, and platforms need configuring.

**Security claims stay honest.** Don't describe the Docker backend as isolation or sandboxing in a security sense. The Firecracker backend does make a real claim now, and it is scoped to rooms with no network — if you change what a room can reach, the claim gets re-scoped in the same PR rather than quietly outgrown. See [SECURITY.md](SECURITY.md).

## What 1.0 promises

Versioning follows [Semantic Versioning](https://semver.org/). As of 1.0
that stops being a formality: **the interfaces below will not break on a
minor release.** A breaking change to any of them needs a major bump.

Three things are covered, because they are the three ways anything outside
this repository depends on keycard:

1. **The configuration file.** Every key documented in
   [keycard.example.toml](keycard.example.toml) — the top-level settings,
   `[rooms.*]`, and the `[firecracker]` table. A key may gain values or
   stop being required; it will not change meaning or disappear.
2. **The command line.** `keycard up` and `keycard rooms`, their flags, and
   the two reserved exit statuses (`124` idle-reaped, `143` shutdown).
3. **The SSH-facing behaviour.** The username selects the room,
   public-key authentication only, `exec` refused, one channel per
   connection, and a room destroyed when the connection ends.

Explicitly **not** covered, so that the promise stays worth something:

- **The Python API.** `import keycard` gets you a version string. Nothing
  under `keycard.*` is a supported library interface, and the backend
  seam in particular is expected to keep moving.
- **The microVM backend's completeness.** `backend = "firecracker"` is
  stable in the sense above — its config keys and behaviour won't break —
  but it is still opt-in and its networking is deliberately unbuilt. See
  [FIRECRACKER.md](FIRECRACKER.md).
- **Log output and human-readable messages.** Don't parse them.
- **The security claims themselves**, which are governed by
  [SECURITY.md](SECURITY.md) rather than by semver. If what a room can
  reach changes, the claim gets re-scoped in the same PR — a narrowing
  there is a correction, not a breaking change.

The last pre-1.0 breaking change was refusing `--keep` on firecracker
rooms, made deliberately before the freeze: it was the only outstanding
question that could have forced a major bump afterwards.

## Releasing

Every notable change belongs in [CHANGELOG.md](CHANGELOG.md) under
`## [Unreleased]` as it lands, not saved up for release day.

To cut a release:

1. In `CHANGELOG.md`, retitle `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
   and add a fresh empty `## [Unreleased]` above it.
2. Bump `version` in `pyproject.toml` and `__version__` in
   `src/keycard/__init__.py` to match, then run `pytest
   tests/test_version_strings.py` and fix whatever it names. The version is
   also written into `README.md`, `man/keycard.1` and both pages under
   `docs/`, and those used to be missed every time — the workflow only ever
   checked the first two. The test fails rather than trusting this list to
   stay complete.
3. Commit those changes, then tag and push:
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
