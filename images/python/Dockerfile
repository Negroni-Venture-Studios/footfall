# The Python base image for apps under test. Footfall starts an app in it with
# the root file system read-only, so installs go into /workspace.

FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50

RUN groupadd --gid 10010 app \
 && useradd --uid 10010 --gid 10010 --home-dir /workspace --no-create-home --shell /usr/sbin/nologin app \
 && install --directory --owner 10010 --group 10010 /workspace

ENV PIP_USER=1 \
    PYTHONUSERBASE=/workspace/.local \
    PIP_NO_CACHE_DIR=1 \
    PATH=/workspace/.local/bin:$PATH

USER 10010:10010
WORKDIR /workspace
