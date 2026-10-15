# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Include the affected component, reproduction steps, expected impact, and any suggested mitigation. Do not include credentials or private user data.

Please do not disclose the issue publicly until a fix or coordinated disclosure plan is available. Ordinary bugs that do not create a security or privacy risk can use the public issue tracker.

## Supported versions

Certus is under active development. Security fixes are applied to the current default branch; there is no maintained stable release line yet.

## Known dependency advisory

As of 2026-10-15, `bun audit` reports [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm) in `braces@3.0.3`, reached through `eslint-config-next`, `@next/eslint-plugin-next`, `fast-glob`, and `micromatch`. The advisory lists no patched version. This dependency path belongs to lint tooling. Avoid supplying untrusted glob patterns to that tooling. The finding remains visible in the full audit, with no advisory ignore or severity reduction. Recheck upstream before a production release.

## Deployment warning

The repository's Docker Compose configuration is for local development. Before exposing Certus to a network or using it for sensitive data, provide production-grade secret management, TLS, least-privilege service identities, network isolation, backups, monitoring, and incident response. PostgreSQL, Redis, Neo4j, Temporal, object storage, and the internal Python services should not be public endpoints.
