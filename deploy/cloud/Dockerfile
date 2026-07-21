# Cortex solo-cloud image (docs/CLOUD_MIGRATION.md P3).
# ONE image, three roles selected by command in the Container App spec:
#   core     : python /app/core/src/headless_main.py   (single corpus writer + loop)
#   gateway  : python -m uvicorn cortex_gateway.app:app (public surface, ATTACH mode)
#   litestream: /app/litestream-replicate.sh           (sidecar; restore runs as an
#                                                       INIT container via
#                                                       /app/litestream-restore.sh)
#
# Build context is assembled by stage.py (sibling checkouts of cortex-core
# and cortex-gateway; data dirs excluded). Build remotely with:
#   az acr build --registry <acr> --image cortex-solo:<tag> <staged-dir>

FROM python:3.12-slim

# Headless engine deps only; the Pi's requirements.txt (BLE/audio/display)
# does not apply here. Gateway deps mirror its requirements.txt minus
# pymssql (Azure SQL retires in this topology).
RUN pip install --no-cache-dir \
    sqlite-vec requests httpx \
    fastapi "uvicorn[standard]" mcp pydantic sqlalchemy

# Litestream for SQLite replication to Azure Blob.
ADD https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz /tmp/ls.tar.gz
RUN tar -C /usr/local/bin -xzf /tmp/ls.tar.gz litestream && rm /tmp/ls.tar.gz

WORKDIR /app
COPY core/ /app/core/
COPY gateway/ /app/gateway/
COPY litestream.yml /etc/litestream.yml
COPY litestream-restore.sh /app/litestream-restore.sh
COPY litestream-replicate.sh /app/litestream-replicate.sh
RUN chmod +x /app/litestream-restore.sh /app/litestream-replicate.sh

ENV PYTHONUNBUFFERED=1

# Default command = core. The gateway and litestream containers override
# the command in the Container App template.
CMD ["python", "/app/core/src/headless_main.py"]
