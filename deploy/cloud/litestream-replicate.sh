#!/bin/sh
# Sidecar: continuous replication of the three SQLite files to Blob.
# Restore already happened in the init container; this only streams.
exec litestream replicate
