# The Node base image for apps under test. Footfall starts an app in it with
# the root file system read-only, so npm caches into /tmp and installs go into
# /workspace.

FROM node:26-slim@sha256:abe7e5a82b61a8ef67b315b85497c9e65173387ccf2f4a029248184b8e020be5

# The base image's own node user has uid 1000; every app runs as 10010.
RUN groupadd --gid 10010 app \
 && useradd --uid 10010 --gid 10010 --home-dir /workspace --no-create-home --shell /usr/sbin/nologin app \
 && install --directory --owner 10010 --group 10010 /workspace

ENV npm_config_cache=/tmp/npm

USER 10010:10010
WORKDIR /workspace
