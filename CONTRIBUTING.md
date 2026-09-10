# Contributing to Certus

Certus welcomes focused bug fixes, tests, documentation, and well-scoped features. Evidence integrity, tenant isolation, and durable recovery are part of the product contract; changes in those areas need tests that prove both the success and failure paths.

## Before you start

- Search existing issues and pull requests before opening a duplicate.
- Open an issue before a large architectural change so the scope can be agreed first.
- Do not include private documents, credentials, customer data, or generated secrets in a fixture.
- Keep migrations append-only. Never edit a migration that may already have been applied.

## Local workflow

```bash
git clone https://github.com/sandeepbist/Certus.git
cd Certus
cp .env.example .env
make setup
make dev
```

Create a branch from the current default branch and keep commits narrow enough to review independently.

Before opening a pull request, run:

```bash
make check
```

When a change affects the optimized applications, also run:

```bash
bun run build:gateway
bun run build:web
```

## Pull requests

A useful pull request includes:

- the problem and the observable behavior it changes;
- the reasoning behind the implementation;
- tests for regressions, invalid input, and authorization boundaries where relevant;
- migration and rollback notes for schema changes;
- screenshots for visible UI changes;
- documentation updates when commands or configuration change.

Avoid drive-by formatting or unrelated refactors in the same pull request. Maintainers may ask for a smaller patch when review or rollback would otherwise be difficult.

## Commit style

Use short, imperative subjects with an area when useful:

```text
fix(retrieval): preserve exact title scope
feat(chat): add bounded session context
docs: clarify local provider modes
```

## Reporting security problems

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md) instead.
