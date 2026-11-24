# Contributing to Footfall

Thank you for helping with a project that runs other people's code and lets a model drive a browser against it, where the sandbox comes before features.

## Before you start

- Read [docs/design.md](docs/design.md) for what Footfall is and the decisions behind it. Planned work is in the [issues labelled enhancement](https://github.com/Negroni-Venture-Studios/footfall/issues?q=is%3Aissue+is%3Aopen+label%3Aenhancement).
- For anything larger than a small fix, open an issue first so we can agree the approach.
- Report security problems privately, as [SECURITY.md](SECURITY.md) describes, never in a public issue.

## Setting up

You need [uv](https://docs.astral.sh/uv/) and git. uv installs the right Python for you.

```bash
git clone https://github.com/Negroni-Venture-Studios/footfall.git
cd footfall
uv sync
uv run pre-commit install
```

The tests never reach the internet. Tests that start containers will need Docker with gVisor on Linux, and will run in their own workflow. For development on a Mac, where Docker Desktop has no gVisor, the runner will accept `FOOTFALL_RUNTIME=runc`, with a warning from every command and no isolation tests. Never set it on a server.

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

- Every change arrives through a pull request. Nobody pushes to `main` directly, maintainers included.
- These CI checks must pass, on a branch that is up to date with `main`:
  - `lint`
  - `typecheck`
  - the `test` jobs
  - `security`
  - `dependency-audit`
  - `sbom`
  - the CodeQL `analyze` jobs
- Force pushes and deletion of `main` are blocked.
- CI is the gate; no approving review count is required. Stale reviews are dismissed when new commits arrive.

Merge with a squash or a merge commit. Branches are deleted after merging.

## Standards

- **Test first.** Write the test that proves your change, watch it fail for the reason you expect, then make it pass. A test that passes both before and after a change proves nothing about it.
- **Keep to the security requirements.** [AGENTS.md](AGENTS.md) lists them: only the runner talks to Docker, container settings come from one file, no secrets in containers, tools that take paths and never URLs, an append-only conversation, tester text labelled as observations, no secrets in logs, and no test on the internet. Each gets tests as it is built; keep them passing and extend them.
- **Type hints throughout**, checked by mypy in strict mode.
- **British English** in documentation and interface text, in sentence case, with no em or en dashes.
- **Commit messages** use the imperative mood ("Add the egress proxy", not "Added …"), with a first line under 72 characters.
- **Changelog.** Add a line under `[Unreleased]` in CHANGELOG.md for anything a user or operator would notice.

## Dependencies

Footfall keeps its dependencies few and well maintained.

- **Ask before adding one.** Open an issue or say so in the pull request, with the reason and its licence.
- Every dependency, direct or transitive, must have a licence compatible with distributing Footfall as MIT. `scripts/check_licences.py` enforces this in CI. GPL and AGPL are ruled out.
- Pin new tools and actions to released versions, and actions by commit SHA. Dependabot's pull requests are reviewed and merged as they arrive, once CI is green.

## Code of conduct

Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Licence

By contributing, you agree that your contributions are licensed under the [MIT licence](LICENSE).
