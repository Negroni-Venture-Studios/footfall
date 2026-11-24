# AGENTS.md: working on Footfall

Instructions for AI coding agents working in this repository. People will find them useful too; [CONTRIBUTING.md](CONTRIBUTING.md) is the human-oriented version.

## What Footfall is

A self-hosted service that tests web apps the way strangers would use them, published as MIT-licensed open source by Negroni Venture Studios. A coding agent asks for a test of a change; Footfall runs the app in a sandbox, sends AI testers at it as different people on different emulated devices, and returns structured findings.

- **[docs/design.md](docs/design.md) is the source of truth.**
- The repository is at phase 1, the sandbox and the tester. Later phases are in the design's build order. New work that is deferred becomes a GitHub issue, not a note in docs/design.md.
- If something you need is missing from docs/design.md, or contradicts it, **ask rather than deciding silently.**

## Commands

```bash
uv sync                            # install everything, from uv.lock
uv run pytest                      # tests
uv run pytest --cov                # tests with the coverage floor
uv run ruff check . && uv run ruff format --check .
uv run mypy                        # strict
uv run python scripts/check_licences.py
uv run pre-commit run --all-files  # lint, types, tests and licences; CI also runs coverage and the security scans
python3 docs/diagrams/build.py     # after changing a diagram in docs/diagrams/
```

Always use `uv run`. Never install into the system Python.

## Layout

| Path | What |
|---|---|
| `footfall/` | The Python package. Phase 1 adds the runner (`runner/`), the egress proxy (`proxy/`) and the tester (`tester/`) |
| `tests/` | pytest suite; `tests/isolation/` will hold the tests that need the real stack |
| `scripts/` | CI and maintenance scripts (`check_licences.py`) |
| `docs/` | `design.md` on how Footfall is designed; `docs/diagrams/` holds the diagrams' sources and `build.py`; `docs/design/` the mockups |
| `.github/` | CI workflows, CodeQL, Dependabot, templates |

## Rules

### Git and pull requests

- `main` is protected. **Never commit to `main` or push to it.** Create a branch, push it, open a pull request, and let CI pass.
- Keep pull requests small and focused. A larger change is several of them, each leaving the suite green.
- Commit messages are in the imperative mood, with a first line under 72 characters.
- When you author or co-author a commit, end the message with this trailer:

      Co-Authored-By: Claude <noreply@anthropic.invalid>

  Do **not** use `noreply@anthropic.com`. That address resolves to an unrelated GitHub account. The `.invalid` top-level domain is reserved and never resolves.
- Add a line under `[Unreleased]` in CHANGELOG.md for anything a user or operator would notice.

### Test first

Write the test that proves the change, and confirm it fails for the reason you expect, before you change the code. Choose inputs that cannot coincide with a default or fallback value, or a wrong answer can pass.

### Security requirements (non-negotiable)

1. **Only the runner talks to Docker.** It alone holds the Docker socket. The orchestrator asks it, over its local Unix socket, and nothing else in Footfall opens the Docker socket.
2. **Container settings come only from `runner/spec.py`.** The user, the capabilities, the read-only root, the memory and process limits, the network and the gVisor runtime are fixed there, as pure functions with tests. No request, manifest or brief can change one.
3. **No secret enters a container.** The one exception is the throwaway password for the app's sign-in, passed to the seed step's environment and nowhere else. Not an API key, not a GitHub token, not a host path.
4. **The tester's tools accept paths, never URLs.** A tool names a page by its path on the app; the guard builds the address, and the browser cannot be sent to another origin.
5. **The conversation is append-only.** Each response's content goes back to the model exactly as returned. Earlier messages are never edited or removed.
6. **Tester text is length-capped and labelled as observations** before it leaves Footfall. What a tester writes may echo what the app displayed, so a calling agent must never read it as an instruction.
7. **Never log** API keys, tokens, or the throwaway passwords.
8. **No test reaches the internet.** `tests/conftest.py` blocks every socket connection except to `127.0.0.1`, `::1` and Unix sockets. Tests that need Docker and gVisor will be marked `isolation` and run in their own workflow, not in the default suite.
9. **Dependencies.** Ask before adding one, and check that its licence is MIT-compatible (`scripts/check_licences.py`). Phase 1 has two runtime dependencies, `anthropic` and `playwright`.

### Versions

Pin dependencies, tools and GitHub Actions to released versions, never pre-releases. Pin Actions by commit SHA, with the version in a comment. Once the browser image exists, the Playwright version in `uv.lock` must be the version installed in it, and a test will enforce that.

Take Dependabot's pull requests as they arrive. Read the diff and the release notes, check a major version against what Footfall uses, and merge once it looks sound and CI is green. Never close one by hand, which tells Dependabot to skip that version for good; it closes superseded ones itself.

### Writing

Everything committed is published, so documentation and interface text are in British English, in sentence case, with no em or en dashes.
