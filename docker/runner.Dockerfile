# The trusted runner: FaultForge core and k6. Run scripts/build_images.sh, which verifies k6 first.
FROM python:3.12-slim AS build
COPY docker/runner-requirements.txt /tmp/requirements.txt
COPY pyproject.toml README.md /tmp/faultforge/
COPY src /tmp/faultforge/src
COPY .tools/bin/k6 /usr/local/bin/k6
RUN pip install --no-cache-dir --no-deps --root-user-action=ignore -r /tmp/requirements.txt /tmp/faultforge \
    && pip check \
    && chmod 0755 /usr/local/bin/k6 \
    && rm -r /tmp/requirements.txt /tmp/faultforge

# See target.Dockerfile: this drops the ENV inherited from the base image.
FROM scratch
COPY --from=build / /
ENV PATH=/usr/local/bin:/usr/bin:/bin
USER 65534:65534
