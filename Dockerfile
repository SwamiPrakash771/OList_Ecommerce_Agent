# One image for the whole app: the React site and the Python API.
# It holds no secrets and no environment-specific settings: those arrive as environment variables when a
# container starts (in AWS from Secrets Manager and Parameter Store, see docs/DEPLOY_AWS.md).
#   API:        default command
#   scheduler:  python -m ai.scheduler run

# ---- 1. build the React site ------------------------------------------------------------------
FROM node:22.12.0-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- 2. the Python app ------------------------------------------------------------------------
FROM python:3.12.8-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Amazon's public CA bundle, to verify the RDS database's certificate (MYSQL_SSL_CA points here)
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/rds/global-bundle.pem

COPY . .
COPY --from=web /web/dist ./web/dist

# The git commit this image was built from: shown by /api/health
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

RUN useradd --system --uid 10001 --home /app app && chown -R app /app
USER app

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
