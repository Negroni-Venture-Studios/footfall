# CLAUDE.md

The working rules for this repository are in AGENTS.md. This file imports it so that every agent reads the same instructions.

@AGENTS.md

## Claude Code specifics

- Run the checks with `uv run` and never with the system Python. `uv run pre-commit run --all-files` is the quick pass before a commit.
- Docker Desktop on a Mac has no gVisor. For development there the runner accepts `FOOTFALL_RUNTIME=runc` and starts containers without gVisor. Footfall warns you while it is set. The isolation tests refuse to run at all. Never set it on a server.
- After changing a diagram's source in `docs/diagrams/`, rebuild with `python3 docs/diagrams/build.py`. Do not edit the built files in `docs/images/diagrams/` by hand.
- After changing a mockup in `docs/design/mockups/`, draw its images again with `uv run python docs/design/render.py`. Do not edit the images in `docs/images/mockups/` by hand.
