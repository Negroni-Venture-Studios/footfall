# CLAUDE.md

The working rules for this repository are in AGENTS.md, imported here so that every agent reads the same instructions.

@AGENTS.md

## Claude Code specifics

- Run the checks with `uv run`, never the system Python. `uv run pre-commit run --all-files` is the quick pass before a commit.
- On a Mac, Docker Desktop has no gVisor. Setting `FOOTFALL_RUNTIME=runc` lets the runner start containers without it for development; every command then prints a warning and the isolation tests refuse to run. Never set it on a server.
- After changing a diagram's source in `docs/diagrams/`, rebuild with `python3 docs/diagrams/build.py`. Do not edit the built files in `docs/images/diagrams/` by hand.
