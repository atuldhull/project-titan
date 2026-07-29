# TITAN backup sidecar (Vol 9 §8.2, TITAN-OPS-018). A thin image with exactly the tools
# the ops/scripts backup path needs: pg_basebackup/psql (postgres base), age (encryption),
# aws-cli (S3/MinIO), redis-cli (RDB), curl+python3 (Qdrant snapshot API). Digest-pin the
# base in production per OPS-005; the floating tag here matches the local Compose stack.
FROM postgres:16-alpine

RUN apk add --no-cache bash coreutils age aws-cli redis curl python3

COPY ops/scripts /opt/titan/ops/scripts
COPY infra/backup/backup-sidecar.sh /opt/titan/backup-sidecar.sh
RUN chmod +x /opt/titan/backup-sidecar.sh /opt/titan/ops/scripts/*.sh

# Runs as the postgres image's unprivileged user; writes only to mounted volumes + the store.
ENTRYPOINT ["/opt/titan/backup-sidecar.sh"]
