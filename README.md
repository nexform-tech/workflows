This repository hosts the reusable GitHub Actions workflows that NexForm repositories call, so release automation is defined once and versioned independently of the skills plugin.

# NexForm workflows

## Contents

| Workflow | Purpose | Used by |
|---|---|---|
| `.github/workflows/release.yml` | Computes the next version from Conventional Commit prefixes, creates the tag, and creates the GitHub release. It never builds or publishes. | Every NexForm code repository |
| `.github/workflows/release-self.yml` | Releases this repository and moves the `v1` tag. | This repository only |
| `.github/workflows/ci.yml` | Lints the workflow files with actionlint. | This repository only |

## Calling the release workflow

Add a caller at `.github/workflows/release.yml` in the consuming repository:

```yaml
name: Release

on:
  push:
    branches:
      - main
      - master

permissions:
  contents: write
  issues: write
  pull-requests: write

concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false

jobs:
  release:
    uses: nexform-tech/workflows/.github/workflows/release.yml@v1
```

The caller owns build and publish jobs. Add them as siblings of `release` and gate each one on `needs.release.outputs.new_tag != ''`, so a push that releases nothing does not build or publish anything.

The reusable workflow returns one output, `new_tag`: the tag created by the run, or an empty string when nothing was released.

## Versioning

Callers pin the moving major tag `@v1`. The `release-self.yml` workflow moves `v1` to the newest release after every release, so a fix reaches every caller without a caller change.

A caller that must stay on an older line can pin a full version such as `@v1.2.3` or a commit SHA.

## Initial version

An untagged repository starts on the `0.x` line: the workflow creates the one-time `v0.0.0` tag, so the first `feat:` release is `0.1.0`. A repository that must keep semantic-release's `1.0.0` first release passes an empty input:

```yaml
    with:
      initial-version: ""
```

This repository itself uses that input, so its first release is `1.0.0`.

## Release rules

The merged commit prefix decides the version: `feat` is a minor, `fix` and `perf` are patches, a `BREAKING CHANGE:` footer is a major, and every other type releases nothing. Never create tags, publish packages, or edit releases by hand; this workflow owns them.

## License

Internal NexForm infrastructure. Contact the NexForm platform team for changes.
