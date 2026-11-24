# The Node base image for apps under test. Footfall starts an app in it with
# the root file system read-only, so npm caches into /tmp and installs go into
# /workspace.

FROM node:22-slim@sha256:be979e5523c76fa4d5ca4ebd3099078f220aa47ec39c48ad5b5df9314debd6eb

# The base image's own node user has uid 1000; every app runs as 10010.
RUN groupadd --gid 10010 app \
 && useradd --uid 10010 --gid 10010 --home-dir /workspace --no-create-home --shell /usr/sbin/nologin app \
 && install --directory --owner 10010 --group 10010 /workspace

ENV npm_config_cache=/tmp/npm

USER 10010:10010
WORKDIR /workspace
