# Security Policy

## Threat model

Please read this before reporting — it defines what counts as a vulnerability.

keycard is a network service that hands out shells. Its security posture depends on which backend is in use.

### v1 (Docker/Podman) — hygiene, not isolation

v1 confines software you broadly trust but don't want permanently installed on your machine. It is **not** a boundary against an actively hostile user.

Containers share a kernel with the host. Container escape is a known and recurring class of vulnerability, and keycard cannot fix that at the container layer.

**In scope for v1:**
- Authentication bypass (connecting without an authorised key)
- Room escape *within keycard's own logic* — e.g. username resolution letting you reach an image you shouldn't
- Host filesystem or Docker socket exposure that keycard introduces
- Sessions or containers surviving disconnect when they should be destroyed
- Resource limits not being applied as configured
- Key material or config being logged or leaked

**Out of scope for v1:**
- Kernel-level container escape
- Denial of service by a user who already holds a valid key
- Anything arising from running keycard as root, exposing it publicly, or granting keys to untrusted parties, all of which the documentation advises against

### v2 (Firecracker) — a real boundary, for rooms with no network

A room opened under `backend = "firecracker"` is a microVM: its own kernel, hardware-virtualized by KVM. Escaping it is not a namespace problem — it means defeating KVM or the device emulation in front of it. **Guest escape is in scope**, and that is the difference between this backend and v1.

The claim holds under conditions that are true of every microVM room today, and each is checked rather than asserted:

- **The guest has no network interface.** Not an isolated subnet, not a firewall — no virtio-net device is ever attached, so the guest kernel has nothing but loopback. Asserted directly in `tests/test_firecracker_integration.py`.
- **The VMM is confined.** firecracker runs as keycard's own unprivileged user, under the seccomp filters it installs itself, and is restricted with Landlock to the room's instance directory, the guest kernel, and `/dev/kvm`. It cannot read the SSH host key or any other room's disk.
- **One channel connects guest to host** — a vsock pair to the guest agent, carrying pty bytes and window size.

If a room ever gains a network, this claim gets re-scoped rather than silently extended.

**In scope for v2**, in addition to everything in scope for v1:
- Guest-to-host escape: running code on the host from inside a room
- Reading another room's data, or the host key, from inside a room
- A compromised VMM reaching host files outside its Landlock allowlist
- Driving the guest agent, or keycard's own vsock handling, into compromising the host process

**Out of scope for v2:**
- Denial of service by a user who already holds a valid key, as in v1
- Transient-execution and other CPU side channels across the guest/host boundary. Firecracker and your host kernel mitigate what they mitigate; keycard adds nothing here and does not claim to
- A host running an unpatched kernel or CPU microcode. The boundary is your host's KVM — keycard cannot make an out-of-date hypervisor safe
- Being root *inside* your own room. That is the product, not a finding

**Two things worth knowing before you rely on this:**

1. **A paused room's memory is written to disk in the clear.** `--keep` snapshots the guest by dumping its RAM to the room's instance directory, so anything that was in that room's memory — keys, tokens, whatever was on screen — sits unencrypted on the host until the keep window expires and the room is reaped. The directory is `0700`, so it is readable only by the user running keycard, but it is not encrypted. If that matters to you, run without `--keep`.
2. **The front door is shared.** Both backends sit behind the same SSH layer. A pre-authentication flaw there is a keycard compromise no matter which backend a room would have used, and no amount of guest isolation helps.

The Docker backend's posture is unchanged by any of this, and it is still the default. See [FIRECRACKER.md](FIRECRACKER.md) for what is built and what is not.

## Reporting a vulnerability

Report privately via [GitHub Security Advisories](https://github.com/SamTruss/keycard/security/advisories/new). Please do not open a public issue for anything in scope above.

Include: what you did, what happened, what you expected, and your backend and version.

Expect an acknowledgement within a few days. This is a side project maintained by one person — timelines are best-effort, and I'd rather set that expectation honestly than miss a stated SLA.

## Deployment guidance

- Public-key authentication only. Password auth is not implemented and won't be.
- Run as an unprivileged user with access to the container socket. Do not run keycard as root — nothing in either backend needs it, and both are designed on that assumption.
- Do not mount host paths or pass the Docker socket into rooms.
- Set `network = "none"` for rooms that don't need outbound access.
- On a shared or internet-facing host, put keycard behind a firewall and treat every authorised key as trusted.
- For the strongest posture available today: `backend = "firecracker"`, `confinement = "required"` under `[firecracker]`, and no `--keep`.
