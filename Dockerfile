# syntax=docker/dockerfile:1.7
# Multi-stage build for CIT VM Accessor
# Stage 1: base runtime (no separate build needed, pure Python)
FROM python:3.12-slim AS runtime

# Prevent Python from writing .pyc and enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Default app port (overridden by docker-compose.yml)
    PORT=8080

# Install minimal OS deps (curl for healthcheck/debug, ca-certs already present)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 appuser
WORKDIR /app

# Copy requirements separately for better layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip check || true

# Copy application (single-file app plus static assets)
COPY main.py ./
COPY static ./static

# (Optional) Copy templates/static if later split out; currently all inline.

# Expose both common ports (8080 default; 8443 used when PORT overridden)
EXPOSE 8080 8443

# Basic healthcheck hitting /healthz (works once the app is up)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:"${PORT}"/healthz || exit 1

# Drop privileges
USER appuser

# Entrypoint simply runs the embedded waitress runner in main.py
# (PORT env variable controls listening port inside container)
ENTRYPOINT ["python", "main.py"]
