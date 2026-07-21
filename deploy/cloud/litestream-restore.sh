#!/bin/sh
# INIT container: restore the corpus from Blob BEFORE core/gateway start.
# Ordering is the whole point: if the core booted first it would create
# EMPTY DBs, restore would skip (-if-db-not-exists), and the replicate
# sidecar would then overwrite the good replica with the empty DB.
# Running this as an ACA init container makes that impossible.
set -e
mkdir -p /data/plugins/overseer
for db in /data/cortex.db /data/plugins/overseer/overseer.db /data/gateway.db; do
  litestream restore -if-db-not-exists -if-replica-exists "$db"
  echo "restore checked: $db"
done
echo "litestream restore phase complete"
