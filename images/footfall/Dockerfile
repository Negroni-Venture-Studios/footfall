# Footfall's own image. The runner, the egress proxy, and the orchestrator all
# run from it. Build it from the repository root.
#
# Base images are pinned by digest. A build always starts from the same bytes.
# Dependabot opens a pull request when a newer digest is published.

FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

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
FROM python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS runtime

LABEL org.opencontainers.image.source="https://github.com/Negroni-Venture-Studios/footfall" \
      org.opencontainers.image.description="Footfall: tests web apps with AI personas on emulated devices" \
      org.opencontainers.image.licenses="MIT"

# git, from the base image's own Debian release (trixie) and its security
# updates: footfall test copies only the files git tracks. git reads a
# repository someone else owns only where it is told to trust it; the dev
# stack mounts the repository under test at /repo, and nowhere else is trusted.
# libatomic1, without which the Node that Playwright bundles cannot start.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends git libatomic1 \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory /repo

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

# ---------------------------------------------------------------- test
# The isolation tests' orchestrator: the image above with the development
# dependencies and tests/, so that pytest runs in a container on each run's
# network, as footfall test does. Built only with `--target test`, in the
# isolation workflow; deploy/dev/compose.test.yaml uses it.
FROM runtime AS test

USER root
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
RUN uv sync --locked \
 && rm /usr/local/bin/uv
COPY tests ./tests
USER footfall

# ---------------------------------------------------------------- the default
# Last, so that a build without --target is the runtime image.
FROM runtime
