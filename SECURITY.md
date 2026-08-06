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

### v2 (Firecracker) — intended as a real boundary

Once the microVM backend lands, escape from a guest becomes in scope. That claim is not being made until then.

## Reporting a vulnerability

Report privately via [GitHub Security Advisories](https://github.com/SamTruss/keycard/security/advisories/new). Please do not open a public issue for anything in scope above.

Include: what you did, what happened, what you expected, and your backend and version.

Expect an acknowledgement within a few days. This is a side project maintained by one person — timelines are best-effort, and I'd rather set that expectation honestly than miss a stated SLA.

## Deployment guidance

- Public-key authentication only. Password auth is not implemented and won't be.
- Run as an unprivileged user with access to the container socket. Do not run keycard as root.
- Do not mount host paths or pass the Docker socket into rooms.
- Set `network = "none"` for rooms that don't need outbound access.
- On a shared or internet-facing host, put keycard behind a firewall and treat every authorised key as trusted.
