# Security policy

localplaud handles private audio, transcripts, account sessions, and provider
credentials. Please report suspected vulnerabilities privately.

## Report a vulnerability

Use **[Report a vulnerability](https://github.com/skyhong2002/localplaud/security/advisories/new)**
to open a private GitHub security report for the maintainer. Do not disclose an
unpatched vulnerability in a public issue or Discussion.

Include the affected revision, deployment mode, impact, and minimal reproduction
steps using synthetic data. Redact credentials, session cookies, signed URLs,
private paths, account identifiers, and recording content. Never test another
person's deployment or data without their permission.

The maintainer will assess the report and coordinate fixes and disclosure with
the reporter. This community project does not promise a response-time SLA or a
bug bounty.

## Supported revisions

Security fixes land on `main`. There is no maintained release-branch support
matrix yet. Follow security advisories, review changes before upgrading, and
back up your workspace before applying database or deployment changes.

## Deployment guidance

The default server binds to loopback. Before making it reachable over a network,
configure [Web App accounts](docs/accounts.md), a session secret, and HTTPS.
Keep secrets outside Git, explicitly authorize external providers and delivery
destinations, and maintain [workspace backups](docs/backups.md).

See the [security architecture](docs/adr/0006-security-posture.md) and
[deployment guide](docs/deploy.md) for the current boundaries and limitations.
