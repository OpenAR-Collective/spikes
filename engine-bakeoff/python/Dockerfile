# One release image carries the API and the frontend built against it.

# Base images are pinned by digest so that a rebuild starts from the same bytes.
# python:3.13-slim
FROM python@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS backend
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
# Only what the application runs and its tests. No frontend sources, no
# deployment state, no local files.
COPY manage.py pytest.ini ./
COPY config/ config/
COPY wax/ wax/
COPY hivear/ hivear/
COPY tests/ tests/
# The API's contract, for the frontend build, and the documentation page's assets.
RUN python manage.py export_openapi_schema --api hivear.api.api --output /tmp/openapi.json \
    && python manage.py collectstatic --noinput > /dev/null

# node:22-alpine
FROM node@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
COPY --from=backend /tmp/openapi.json ./openapi.json
ARG RELEASE=0.2.0
# Types are generated from this release's API. A frontend that has not
# absorbed an API change fails here, not in front of a user (SHARED-0009).
RUN npm run generate && VITE_RELEASE=${RELEASE} npm run build

FROM backend
RUN rm /tmp/openapi.json
COPY --from=frontend /frontend/dist /app/frontend_dist
ARG RELEASE=0.2.0
# Empty means the previous minor release, which is the rule. A build states
# a floor only when it has to differ, for example at a major version boundary.
ARG MINIMUM_CODE_RELEASE=""
ENV HIVEAR_RELEASE=${RELEASE} \
    HIVEAR_MINIMUM_CODE_RELEASE=${MINIMUM_CODE_RELEASE}
RUN useradd --system --no-create-home hivear
USER hivear

# Port 8000 takes live traffic. Port 8001 is the preview listener: the
# application refuses to write for anything that arrives there.
CMD ["gunicorn", "config.wsgi", "--bind", "0.0.0.0:8000", "--bind", "0.0.0.0:8001", "--workers", "3", "--access-logfile", "-"]
