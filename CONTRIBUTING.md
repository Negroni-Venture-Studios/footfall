# Contributing to Footfall

Thank you for helping with Footfall. Footfall runs other people's code and lets a model drive a browser against it. Its sandbox comes before its features.

## Before you start

- Read [docs/design.md](docs/design.md) for what Footfall is and the decisions behind it. Planned work is in the [issues labelled enhancement](https://github.com/Negroni-Venture-Studios/footfall/issues?q=is%3Aissue+is%3Aopen+label%3Aenhancement).
- For anything larger than a small fix open an issue first so we can agree the approach.
- Report security problems privately and never in a public issue. [SECURITY.md](SECURITY.md) says how.

## Setting up

You need [uv](https://docs.astral.sh/uv/) and git. uv installs the right Python for you.

```bash
git clone https://github.com/Negroni-Venture-Studios/footfall.git
cd footfall
uv sync
uv run pre-commit install
```

The tests never reach the internet. The tests that start containers need Docker with gVisor on Linux. They run only in the isolation workflow (`.github/workflows/isolation.yml`). Docker Desktop on a Mac has no gVisor. For development there the runner accepts `FOOTFALL_RUNTIME=runc`. Footfall warns you while it is set. The isolation tests refuse to run at all. Never set it on a server.

## Everyday commands

```bash
uv run pytest                      # tests
uv run pytest --cov                # tests with the coverage floor
uv run ruff check .                # lint, including security rules
uv run ruff format .               # format
uv run mypy                        # strict type checking
uv run python scripts/check_licences.py
uv run pre-commit run --all-files  # the fast checks; CI also runs coverage and the security scans
python3 docs/diagrams/build.py     # after changing a diagram in docs/diagrams/
```

## How a change gets into `main`

`main` is protected:

- Every change arrives through a pull request. Nobody pushes to `main` directly and that includes me.
- These checks must pass on a branch that is up to date with `main`:
  - `lint`
  - `typecheck`
  - `test (3.12)` and `test (3.14)`
  - `security`
  - `dependency-audit`
  - `sbom`
  - `images`: a build of the four container images
  - CodeQL's `analyze (python)` and `analyze (actions)`
  - `isolation`: the sandbox's own tests under gVisor
- Every conversation on the pull request must be resolved.
- Force pushes and deletion of `main` are blocked.
- CI is the gate. No approving review count is required. Stale reviews are dismissed when new commits arrive.

Merge with a squash or a merge commit. Branches are deleted after merging.

## Standards

- **Test first.** Write the test that proves your change, watch it fail for the reason you expect, then make it pass. A test that passes both before and after a change proves nothing about it.
- **Keep to the security requirements.** [AGENTS.md](AGENTS.md) lists them: only the runner talks to Docker, container settings come from one file, no secrets in containers, tools that take paths and never URLs, an append-only conversation, tester text labelled as observations, no secrets in logs, and no test on the internet. Every requirement gets tests as it is built. Keep them passing and extend them.
- **Type hints throughout.** mypy checks them in strict mode.
- **British English** in documentation and interface text. Write it in sentence case with no em or en dashes.
- **Commit messages** are in the imperative mood. Write "Add the egress proxy", not "Added the egress proxy". Keep the first line under 72 characters.
- **Changelog.** Add a line under `[Unreleased]` in CHANGELOG.md for anything a user or operator would notice.

## Dependencies

Footfall keeps its dependencies few and well maintained.

- **Ask before adding one.** Open an issue or say so in the pull request. Give the reason and the dependency's licence.
- Every direct or transitive dependency must have a licence compatible with distributing Footfall as MIT. `scripts/check_licences.py` enforces this in CI. GPL and AGPL are ruled out.
- Every direct or transitive package Footfall installs to run has its own row in [THIRD_PARTY_LICENCES.md](THIRD_PARTY_LICENCES.md). A test fails until a new one is added there.
- Pin new tools and actions to released versions. Pin actions by commit SHA. I review and merge Dependabot's pull requests as they arrive once CI is green.

### Updating Playwright

Playwright's server in the browser image refuses a client of another release. `uv.lock` and `images/browsers/Dockerfile` move together and `tests/test_images.py` fails while they differ. Dependabot opens a separate pull request for each Playwright release. It does not group them with the weekly updates and never moves the browser image itself. That pull request fails the image test until someone updates the browser image in it:

1. Wait until Microsoft has published the image for the new release, `mcr.microsoft.com/playwright:v<version>-noble`. It can appear a day or two after the release on PyPI.
2. On Dependabot's branch change the base image in `images/browsers/Dockerfile` to the new release's tag with the digest that `docker buildx imagetools inspect mcr.microsoft.com/playwright:v<version>-noble` prints. Change `npm install --global playwright@<version>` to match.
3. Push to the same branch. Dependabot stops rebasing a pull request once someone else has pushed to it. Bring the branch up to date with `main` by hand if it falls behind.

The pull request then moves `uv.lock` and the browser image together. It is merged once every check is green.

## Code of conduct

Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Licence

By contributing you agree that your contributions are licensed under the [MIT licence](LICENSE).
