# Adapted from the HiveAR prototype by Andy Troiano
# (https://github.com/atroiano/platform-design, commit 361b6aa,
# prototype/Dockerfile), Apache License 2.0.
# Modified by The OpenAR Collective contributors.
# Changes: one Python stage. The frontend build, the OpenAPI export, the
# static files, and the gunicorn web server are removed with the HTTP layer,
# the default process is the projection reader, and the image carries the
# bake-off's own packages, bakeoff and direct (Foundation-written).

# Base images are pinned by digest so that a rebuild starts from the same bytes.
# python:3.13-slim
FROM python@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
# Only what the engine runs and its tests. No deployment state, no local files.
COPY manage.py pytest.ini ./
COPY config/ config/
COPY wax/ wax/
COPY hivear/ hivear/
COPY bakeoff/ bakeoff/
COPY direct/ direct/
COPY tests/ tests/
ARG RELEASE=0.2.0
# Empty means the previous minor release, which is the rule. A build states
# a floor only when it has to differ, for example at a major version boundary.
ARG MINIMUM_CODE_RELEASE=""
ENV HIVEAR_RELEASE=${RELEASE} \
    HIVEAR_MINIMUM_CODE_RELEASE=${MINIMUM_CODE_RELEASE}
RUN useradd --system --no-create-home hivear
USER hivear

# The engine's long-running process keeps the read models current (WAX-0001).
# Commands are dispatched in-process by whatever drives the engine, such as
# the tests or the bake-off harness.
CMD ["python", "manage.py", "run_projections"]
