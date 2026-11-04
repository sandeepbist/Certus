# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Include the affected component, reproduction steps, expected impact, and any suggested mitigation. Do not include credentials or private user data.

Please do not disclose the issue publicly until a fix or coordinated disclosure plan is available. Ordinary bugs that do not create a security or privacy risk can use the public issue tracker.

## Supported versions

Certus is under active development. Security fixes are applied to the current default branch; there is no maintained stable release line yet.

## Known dependency advisory

As of 2026-11-02, `bun audit` reports [GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249) in the locked `minimatch` versions 3.1.5 and 10.2.6. The advisory lists 11.0.1 as the first patched version. ESLint, its plugins, and TypeScript ESLint require older major versions, so a compatible lockfile refresh cannot apply that fix. Avoid supplying untrusted glob patterns to lint tooling. The finding remains visible in the full audit, with no advisory ignore or severity reduction. Recheck the parent packages before a production release.

The previously documented `braces` advisory, [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm), was withdrawn upstream on 2026-10-23. Its absence from the current audit does not represent a local suppression or package fix.

## Deployment warning

The repository's Docker Compose configuration is for local development. Before exposing Certus to a network or using it for sensitive data, provide production-grade secret management, TLS, least-privilege service identities, network isolation, backups, monitoring, and incident response. PostgreSQL, Redis, Neo4j, Temporal, object storage, and the internal Python services should not be public endpoints.
