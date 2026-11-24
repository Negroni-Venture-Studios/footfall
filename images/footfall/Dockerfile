# Footfall's own image: the runner, the egress proxy and the orchestrator all
# run from it. Build from the repository root, as with Snug's image.
#
# Base images are pinned by digest, so a build always starts from the same
# bytes; Dependabot proposes new digests.

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

# ---------------------------------------------------------------- build
FROM python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS build

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY footfall ./footfall
# uv compiles the dependencies; Footfall itself is installed from /app/footfall,
# so it is compiled here. The root file system is read-only, so without this
# every start would compile Footfall's code again.
RUN uv sync --locked --no-dev \
 && /app/.venv/bin/python -m compileall -q --invalidation-mode unchecked-hash /app/footfall

# ---------------------------------------------------------------- runtime
FROM python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

LABEL org.opencontainers.image.source="https://github.com/Negroni-Venture-Studios/footfall" \
      org.opencontainers.image.description="Footfall: tests web apps with AI personas on emulated devices" \
      org.opencontainers.image.licenses="MIT"

# git, from the base image's own Debian release (trixie) and its security
# updates: footfall test copies only the files git would commit.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# The two socket folders are mount points for volumes, which take their owner
# and mode from here: the runner's socket, which the orchestrator's group may
# reach, and the proxy's control socket, which only the proxy's user and root
# may reach.
RUN groupadd --system --gid 10001 footfall \
 && useradd --system --uid 10001 --gid footfall --home-dir /var/lib/footfall --shell /usr/sbin/nologin footfall \
 && install --directory --owner footfall --group footfall --mode 0700 /var/lib/footfall \
 && install --directory --owner root --group footfall --mode 0750 /run/footfall \
 && install --directory --owner footfall --group footfall --mode 0700 /run/footfall-proxy

COPY --from=build --chown=root:root /app /app
# The base image ships Python's standard library without compiled files, and
# the root file system is read-only, so every start would compile what it used.
RUN python -m compileall -q --invalidation-mode unchecked-hash \
    "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Compose starts the runner as root, because it holds the Docker socket. The
# image itself defaults to the unprivileged user.
USER footfall
WORKDIR /var/lib/footfall

# `footfall test`, `footfall runner` or `footfall proxy`; deploy/dev/compose.yaml
# runs all three.
ENTRYPOINT ["footfall"]
