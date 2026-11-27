# Unified zero-dependency container image for both SMB and Enterprise profiles.
# Select role at runtime with DEPLOYMENT_PROFILE=smb | gate | worker | enterprise.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BIND_ADDRESS=0.0.0.0 \
    PORT=8080 \
    DEPLOYMENT_PROFILE=smb \
    ENTITLEMENTS_PATH=/app/entitlements.example.json

RUN useradd --create-home --shell /usr/sbin/nologin appuser
WORKDIR /app

COPY gcp_auth.py looker_api.py entitlements.py ledger.py task_dispatcher.py \
     log_parser.py provisioner.py server.py entitlements.example.json ./
USER appuser

EXPOSE 8080
CMD ["python3", "server.py"]
