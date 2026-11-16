# The Node toolchain for a stack built from source (docs/PLAN.md M11). Two targets:
#   build  node, npm and one pinned pnpm, with /deps owned by the build user. Installs and builds run here.
#   run    node alone: no npm, corepack or yarn. A built app's image starts FROM this.
# Run scripts/build_node_images.sh, which reads the pnpm checksum from docker/pnpm.lock.
ARG NODE_VERSION
FROM node:${NODE_VERSION}-alpine3.24 AS with-pnpm
ARG PNPM_VERSION
ARG PNPM_SHA512
# The tarball is checked before anything installs it, and no install script runs.
RUN cd /tmp \
    && npm pack "pnpm@${PNPM_VERSION}" --registry https://registry.npmjs.org/ >/dev/null \
    && echo "${PNPM_SHA512}  pnpm-${PNPM_VERSION}.tgz" | sha512sum -c - \
    && npm install -g --ignore-scripts "./pnpm-${PNPM_VERSION}.tgz" \
    && rm -rf "pnpm-${PNPM_VERSION}.tgz" /root/.npm /tmp/* \
    && mkdir /deps && chown 1000:1000 /deps

# See target.Dockerfile: copying into an empty image drops the ENV inherited from the base.
FROM scratch AS build
COPY --from=with-pnpm / /
ENV PATH=/usr/local/bin:/usr/bin:/bin
USER 1000:1000

FROM node:${NODE_VERSION}-alpine3.24 AS node-only
RUN rm -rf /usr/local/lib/node_modules /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
    /usr/local/bin/yarn /usr/local/bin/yarnpkg /opt/yarn* /root/.npm /tmp/*

FROM scratch AS run
COPY --from=node-only / /
ENV PATH=/usr/local/bin:/usr/bin:/bin
USER 1000:1000
