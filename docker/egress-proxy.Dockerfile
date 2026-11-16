# The registry-only proxy for a dependency fetch (docs/PLAN.md M11). Standard library only: no pip.
FROM python:3.12-slim AS build
COPY src/faultforge/egress_proxy.py /opt/egress_proxy.py

# See target.Dockerfile: this drops the ENV inherited from the base image.
FROM scratch
COPY --from=build / /
ENV PATH=/usr/local/bin:/usr/bin:/bin
USER 65534:65534
