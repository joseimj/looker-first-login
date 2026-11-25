# Stage 2 container image (e.g. Cloud Run) for the provisioning backend.
# No third-party packages are required; only the Python standard library is used.
#
# Build and deploy example (Cloud Run):
#   gcloud run deploy looker-first-login-poc \
#     --source backend \
#     --region europe-west1 \
#     --set-env-vars LOOKER_BASE_URL=https://instance.cloud.looker.com,ENTITLEMENTS_PATH=/app/entitlements.json,BIND_ADDRESS=0.0.0.0 \
#     --set-secrets LOOKER_CLIENT_ID=looker-client-id:latest,LOOKER_CLIENT_SECRET=looker-client-secret:latest,PROVISIONING_SHARED_SECRET=provisioning-secret:latest

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BIND_ADDRESS=0.0.0.0 \
    PORT=8080 \
    ENTITLEMENTS_PATH=/app/entitlements.example.json

RUN useradd --create-home --shell /usr/sbin/nologin appuser
WORKDIR /app

COPY looker_api.py provisioner.py server.py entitlements.example.json ./
USER appuser

EXPOSE 8080
CMD ["python3", "server.py"]
