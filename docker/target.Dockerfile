# The untrusted target's runtime: Python, FastAPI and uvicorn. Code is mounted at /app for each run.
FROM python:3.12-slim AS build
COPY docker/target-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --root-user-action=ignore -r /tmp/requirements.txt \
    && pip check \
    && rm /tmp/requirements.txt

# An image can't unset an inherited ENV, and the base sets GPG_KEY and others. Copying the files
# into an empty image is what makes the container's environment a true allowlist.
FROM scratch
COPY --from=build / /
ENV PATH=/usr/local/bin:/usr/bin:/bin
WORKDIR /app
USER 65534:65534
