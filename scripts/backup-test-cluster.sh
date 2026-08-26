#!/bin/bash
# Phase 11 slice 0 — build a throwaway cluster that can be backed up.
#
# The backup measurements need a cluster the development one cannot be:
# `archive_mode` is postmaster context, a stanza has to own the whole cluster,
# and the restore measurement stops the postmaster and writes over a data
# directory. So they get their own cluster, on their own port, which this
# script builds and `--drop` destroys.
#
# It is deliberately NOT idempotent-in-place: `--drop` first, then create. A
# cluster half-configured from a previous run is exactly the thing that makes a
# security assertion pass for the wrong reason -- the same rule
# scripts/realtime-test-cluster.sh follows, for the same reason.
#
# The thing this script exists to make testable is the ADR-031 interaction.
# Every MaluDB node carries `host replication all <cidr> reject`, because a
# non-superuser holding REPLICATION took a physical copy of every database on
# the cluster. That reject is also what pg_basebackup needs, so the question
# "can this platform take a backup at all" is a question about that line.
# `--permissive` builds the cluster WITHOUT it, so the difference can be
# measured rather than asserted.
#
# Safe only on a development host. It creates and drops a whole PostgreSQL
# cluster and writes a backup repository. Do not point any of it at a node with
# tenants.
#
# Requires: passwordless sudo, postgresql-17, postgresql-common, pgbackrest.
#
#   scripts/backup-test-cluster.sh              # build it, print the exports
#   scripts/backup-test-cluster.sh --drop       # destroy it and the repository
#   scripts/backup-test-cluster.sh --permissive # build it WITHOUT the ADR-031
#                                               # reject, to show what the
#                                               # reject is actually costing

set -euo pipefail

VERSION="${PG_VERSION:-17}"
CLUSTER="${PG_CLUSTER:-bk}"
PORT="${PG_PORT:-5434}"
STANZA="${BK_STANZA:-maludb-$CLUSTER}"
SUPERUSER_PW="${BK_SUPERUSER_PW:-$(openssl rand -hex 16)}"

# The repository. On this development host it lands on the same filesystem as
# the data directory, which is precisely what the Phase 11 plan proposes to
# forbid in production (a repository sharing a failure domain with the data is
# not a backup). It is fine for measurement and it is not a deployment model.
REPO="${BK_REPO:-/var/lib/pgbackrest}"

# Two full backups is enough to measure retention expiry without spending disk
# on a development box. It is written into the stanza rather than left to the
# default, because pgBackRest has no default: unset, it warns that the
# repository may run out of space and keeps every backup and every WAL segment
# ever archived.
RETENTION_FULL="${BK_RETENTION_FULL:-2}"

# postgres owns /etc/postgresql/<v>/<cluster> and /etc/pgbackrest.conf, and a
# root without CAP_DAC_OVERRIDE cannot write into a directory it neither owns
# nor has group on. Writing as the owner works everywhere and is what this
# script does; `sudo tee` into those paths does not.
as_postgres() { sudo -u postgres "$@"; }

# Remove this stanza's section from /etc/pgbackrest.conf. `--drop` has to do
# this and so does a rebuild: the section is appended, so without it a second
# run leaves two `[$STANZA]` blocks and pgbackrest fails with "option 'pg1-path'
# cannot be set multiple times". Found by rebuilding, which is the only way a
# non-idempotent script's cleanup gets tested.
strip_stanza() {
  as_postgres awk -v s="[$STANZA]" '
    $0 == s { skip = 1; next }
    /^\[/   { skip = 0 }
    !skip   { print }
  ' /etc/pgbackrest.conf > /tmp/pgbackrest.conf.new
  as_postgres cp /tmp/pgbackrest.conf.new /etc/pgbackrest.conf
  rm -f /tmp/pgbackrest.conf.new
}

case "${1:-}" in
  --drop)
    sudo pg_dropcluster --stop "$VERSION" "$CLUSTER" 2>/dev/null || true
    as_postgres rm -rf "${REPO:?}/archive/$STANZA" "${REPO:?}/backup/$STANZA" 2>/dev/null || true
    strip_stanza
    echo "dropped $VERSION/$CLUSTER, the $STANZA repository and its stanza config"
    exit 0
    ;;
  --permissive) PERMISSIVE=1 ;;
  "") PERMISSIVE=0 ;;
  *) echo "usage: $0 [--drop|--permissive]" >&2; exit 2 ;;
esac

command -v pg_createcluster >/dev/null || { echo "pg_createcluster not found" >&2; exit 1; }
command -v pgbackrest      >/dev/null || { echo "pgbackrest not found (apt install pgbackrest)" >&2; exit 1; }

sudo pg_dropcluster --stop "$VERSION" "$CLUSTER" 2>/dev/null || true
as_postgres rm -rf "${REPO:?}/archive/$STANZA" "${REPO:?}/backup/$STANZA" 2>/dev/null || true
strip_stanza
sudo pg_createcluster "$VERSION" "$CLUSTER" --port "$PORT" -- --auth-local=peer >/dev/null

CONF="/etc/postgresql/$VERSION/$CLUSTER/postgresql.conf"
HBA="/etc/postgresql/$VERSION/$CLUSTER/pg_hba.conf"
DATA="/var/lib/postgresql/$VERSION/$CLUSTER"

# The pgBackRest stanza. `pg1-path` is the data directory it copies; the copy is
# a filesystem copy taken between pg_backup_start() and pg_backup_stop() over an
# ordinary libpq connection, which is the whole reason the ADR-031 reject does
# not stop it. Nothing here opens a replication connection.
as_postgres tee -a /etc/pgbackrest.conf >/dev/null <<EOF

[$STANZA]
pg1-path=$DATA
pg1-port=$PORT
pg1-socket-path=/var/run/postgresql
repo1-retention-full=$RETENTION_FULL
# Without this, pgBackRest expires backups and keeps their WAL forever -- it
# says so on every run ("archive logs will not be expired") and the repository
# grows without bound. Set to the same count as the backups, which is the only
# value that makes the archive expire with the backup set it belongs to.
repo1-retention-archive=$RETENTION_FULL
repo1-retention-archive-type=full
EOF

as_postgres tee -a "$CONF" >/dev/null <<EOF

# Phase 11 slice 0 backup measurement cluster. specs/backup-restore-model.md.
wal_level = replica                       # enough for physical backup and PITR
archive_mode = on                         # postmaster context: needs a restart
archive_command = 'pgbackrest --stanza=$STANZA archive-push %p'
archive_timeout = 60                      # bound how stale a PITR target can be
max_wal_senders = 3                       # pgBackRest uses none of these; see below
EOF

if [ "$PERMISSIVE" -eq 0 ]; then
  # ADR-031, finding R7, copied from scripts/realtime-test-cluster.sh so the two
  # clusters are protected identically. The `replication` keyword matches
  # PHYSICAL connections only, so this blocks pg_basebackup while leaving
  # logical decoding working and CONNECT-scoped.
  #
  # It must come BEFORE the default replication lines pg_createcluster writes,
  # or first-match makes it dead text.
  as_postgres sed -i "1i # Phase 11 slice 0 (ADR-031): physical replication is rejected. Logical is not.\nhost    replication     all     127.0.0.1/32    reject\nhost    replication     all     ::1/128         reject" "$HBA"
else
  echo "!! building WITHOUT the ADR-031 reject: this cluster hands a cluster-wide"
  echo "!! reader to any role holding REPLICATION. Measurement fixture only."
fi

sudo pg_ctlcluster "$VERSION" "$CLUSTER" start
as_postgres psql -p "$PORT" -q -v ON_ERROR_STOP=1 \
  -c "ALTER ROLE postgres WITH PASSWORD '$SUPERUSER_PW'" >/dev/null

as_postgres pgbackrest --stanza="$STANZA" stanza-create
as_postgres pgbackrest --stanza="$STANZA" check

echo
echo "cluster $VERSION/$CLUSTER is up on port $PORT, stanza $STANZA"
as_postgres psql -p "$PORT" -At -c \
  "SELECT name || ' = ' || setting FROM pg_settings
    WHERE name IN ('wal_level','archive_mode','archive_timeout','max_wal_senders')
    ORDER BY name" | sed 's/^/  /'
if [ "$PERMISSIVE" -eq 0 ]; then
  echo "  pg_hba: physical replication rejected (ADR-031)"
else
  echo "  pg_hba: physical replication PERMITTED -- fixture only"
fi
echo
echo "export MALUDB_BACKUP_NODE_DSN=\"postgresql://postgres:$SUPERUSER_PW@127.0.0.1:$PORT/postgres\""
echo "export MALUDB_BACKUP_STANZA=$STANZA"
echo
echo "drop it with: $0 --drop"
