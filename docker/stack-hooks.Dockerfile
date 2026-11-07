# The stack hooks helper: trusted reset and state for an app with no test hooks of its own.
FROM python:3.12-slim AS build
COPY docker/stack-hooks-requirements.txt /tmp/requirements.txt
COPY src/faultforge/stack_hooks.py /opt/stack_hooks.py
RUN pip install --no-cache-dir --require-hashes --root-user-action=ignore -r /tmp/requirements.txt \
    && pip check \
    && rm /tmp/requirements.txt

# See target.Dockerfile: this drops the ENV inherited from the base image.
FROM scratch
COPY --from=build / /
ENV PATH=/usr/local/bin:/usr/bin:/bin
USER 65534:65534
