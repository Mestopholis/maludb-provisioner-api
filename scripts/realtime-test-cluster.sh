#!/bin/bash
# Phase 06 slice 1 — build a throwaway cluster that can host Realtime.
#
# The Realtime tests need something the development cluster cannot be: a node
# with `wal_level = logical` and a `pg_hba.conf` under test. Three of the five
# preconditions in specs/realtime-replication-model.md need a cluster restart,
# and restarting the development cluster drops every connection on it. So the
# tests get their own cluster, on their own port, which this script builds and
# `--drop` destroys.
#
# It is deliberately NOT idempotent-in-place: `--drop` first, then create. A
# cluster half-configured from a previous run is exactly the thing that makes a
# security assertion pass for the wrong reason.
#
# Safe only on a development host. It creates and drops a whole PostgreSQL
# cluster, and the R6b test in tests/test_realtime_node.py takes a physical base
# backup — which, on a cluster carrying customer data, produces a readable copy
# of every database on it. Do not point any of this at a node with tenants.
#
# Requires: passwordless sudo, postgresql-17, postgresql-common (pg_createcluster).
#
#   scripts/realtime-test-cluster.sh              # build it, print the DSN
#   scripts/realtime-test-cluster.sh --drop       # destroy it
#   scripts/realtime-test-cluster.sh --permissive # build it WITHOUT the ADR-031
#                                                 # reject, to prove the test
#                                                 # actually fails without it

set -euo pipefail

VERSION="${PG_VERSION:-17}"
CLUSTER="${PG_CLUSTER:-rt}"
PORT="${PG_PORT:-5433}"
SUPERUSER_PW="${RT_SUPERUSER_PW:-$(openssl rand -hex 16)}"

# Small on purpose. The ceiling has to be reachable in a handful of statements
# for the R2 test to be able to hit it, and a bound of 64 MB is the floor
# services/control_plane/realtime.py accepts — enough that ordinary traffic does
# not invalidate a slot, small enough that a deliberate stall does.
MAX_SLOTS="${RT_MAX_SLOTS:-4}"
KEEP_SIZE="${RT_KEEP_SIZE:-64MB}"

# Phase 06 slice 5: the Realtime data address. A Realtime instance is a
# container with no access to the node's loopback -- with it, it reaches every
# other worker on the node, including tenants' PostgREST, which serves anonymous
# reads to anything that can open its port. So PostgreSQL also listens on a
# dedicated address on its own interface, which a container can reach and
# nothing else on the node uses.
DATA_ADDRESS="${RT_DATA_ADDRESS:-10.90.0.1}"
DATA_IFACE="${RT_DATA_IFACE:-maludb-rt}"

PERMISSIVE=0
case "${1:-}" in
  --drop)
    sudo pg_dropcluster --stop "$VERSION" "$CLUSTER" 2>/dev/null || true
    sudo ip link del "$DATA_IFACE" 2>/dev/null || true
    echo "dropped $VERSION/$CLUSTER and the $DATA_IFACE interface"
    exit 0
    ;;
  --permissive) PERMISSIVE=1 ;;
  "") ;;
  *) echo "usage: $0 [--drop|--permissive]" >&2; exit 2 ;;
esac

command -v pg_createcluster >/dev/null || { echo "pg_createcluster not found" >&2; exit 1; }

sudo pg_dropcluster --stop "$VERSION" "$CLUSTER" 2>/dev/null || true
sudo pg_createcluster "$VERSION" "$CLUSTER" --port "$PORT" -- --auth-local=peer >/dev/null

CONF="/etc/postgresql/$VERSION/$CLUSTER/postgresql.conf"
HBA="/etc/postgresql/$VERSION/$CLUSTER/pg_hba.conf"

# The data address, on an interface of its own so that binding PostgreSQL to it
# exposes nothing to the network the node is actually on.
sudo ip link del "$DATA_IFACE" 2>/dev/null || true
sudo ip link add "$DATA_IFACE" type dummy
sudo ip addr add "$DATA_ADDRESS/32" dev "$DATA_IFACE"
sudo ip link set "$DATA_IFACE" up

sudo tee -a "$CONF" >/dev/null <<EOF

# Phase 06 slice 1 test cluster. specs/realtime-replication-model.md.
wal_level = logical                       # R1: nothing works below it
max_replication_slots = $MAX_SLOTS        # R2: low, so the ceiling is reachable
max_wal_senders = $MAX_SLOTS              # R2: a slot no sender can attach to is useless
max_slot_wal_keep_size = $KEEP_SIZE       # ADR-032: the only backstop against a stalled consumer

# Phase 06 slice 5: reachable from a Realtime container, which has no route to
# this node's loopback and must not be given one.
listen_addresses = 'localhost,$DATA_ADDRESS'
EOF

# Whether this minor has `output_plugin_libraries` (17.11+). Read from the
# binary rather than assumed, because older minors refuse to start with a
# setting they do not know. The value is applied after the cluster is up: it
# takes a reload, and it has to be built from the running default rather than
# written blind -- see below.
if "/usr/lib/postgresql/$VERSION/bin/postgres" --describe-config \
     | grep -q '^output_plugin_libraries'; then
  HAS_PLUGIN_ALLOWLIST=1
else
  HAS_PLUGIN_ALLOWLIST=0
fi

if [ "$PERMISSIVE" -eq 0 ]; then
  # ADR-031, finding R7. The `replication` keyword matches PHYSICAL replication
  # connections only: `all` does not match them, and logical replication names a
  # real database and matches the ordinary rules below. So this line blocks
  # pg_basebackup while leaving logical decoding working and CONNECT-scoped.
  #
  # It must come BEFORE the default replication lines, which pg_createcluster
  # writes, or first-match makes it dead text.
  #
  # The data address needs its own reject. ADR-031's containment is per-address,
  # so opening a second address without one re-opens exactly the hole it closed
  # -- and `cp-manage node realtime-check` would report the permissive rule.
  sudo sed -i "1i # Phase 06 slice 1 (ADR-031): physical replication is rejected. Logical is not.\nhost    replication     all     127.0.0.1/32    reject\nhost    replication     all     ::1/128         reject\nhost    replication     all     $DATA_ADDRESS/32  reject\nhost    all             all     $DATA_ADDRESS/32  scram-sha-256" "$HBA"
else
  echo "!! building WITHOUT the ADR-031 reject: this cluster hands a cluster-wide"
  echo "!! reader to any role holding REPLICATION. Test fixture only."
fi

sudo pg_ctlcluster "$VERSION" "$CLUSTER" start
sudo -u postgres psql -p "$PORT" -q -v ON_ERROR_STOP=1 \
  -c "ALTER ROLE postgres WITH PASSWORD '$SUPERUSER_PW'" >/dev/null

if [ "$HAS_PLUGIN_ALLOWLIST" -eq 1 ]; then
  # PostgreSQL 17.11 added this allowlist of the libraries a replication
  # connection may load. An installed wal2json that is not named here is
  # refused, and the refusal is invisible from a client: every subscription
  # succeeds and no event is ever delivered.
  #
  # Appended to the running default rather than set to 'wal2json', because the
  # setting REPLACES rather than adds -- and the default is what permits
  # `pgoutput`, which every Realtime project's second slot uses (ADR-034), and
  # `test_decoding`, which the R1/R2/R4/R8 assertions use. Overwriting it with
  # the one plugin this script came to add breaks the other two, which is how
  # this was found.
  # Written to postgresql.conf and reloaded, NOT via ALTER SYSTEM. This is a
  # list GUC whose elements are quoted -- like shared_preload_libraries -- and
  # `ALTER SYSTEM SET x = 'a, b, c'` gives such a variable a single element
  # named "a, b, c" rather than three. It applies, it shows, and it matches
  # nothing. The config-file form splits on the commas, which is why every
  # example of shared_preload_libraries in the wild is written this way.
  CURRENT=$(sudo -u postgres psql -p "$PORT" -tAc "SHOW output_plugin_libraries")
  CURRENT=${CURRENT//\"/}
  case ",${CURRENT// /}," in
    *,wal2json,*) ALLOWED="$CURRENT" ;;
    *)            ALLOWED="${CURRENT:+$CURRENT, }wal2json" ;;
  esac
  sudo tee -a "$CONF" >/dev/null <<EOF

# 17.11+: the allowlist of libraries a replication connection may load, with
# wal2json appended to this version's default rather than replacing it.
output_plugin_libraries = '$ALLOWED'
EOF
  sudo pg_ctlcluster "$VERSION" "$CLUSTER" reload
fi

# Assert the plugins actually load, here rather than three failures downstream.
# This script exists to build a cluster the Realtime tests can trust, and every
# way of getting the allowlist wrong so far -- absent package, replaced default,
# a list stored as one element -- fails identically from a client: subscribe,
# then nothing. A temporary slot is the only thing that proves the difference.
for plugin in wal2json pgoutput test_decoding; do
  if ! sudo -u postgres psql -p "$PORT" -qtA -v ON_ERROR_STOP=1 \
         -c "SELECT pg_create_logical_replication_slot('build_probe_$plugin', '$plugin', true)" \
         -c "SELECT pg_drop_replication_slot('build_probe_$plugin')" >/dev/null; then
    echo "!! $plugin does not load on this cluster. Slots using it deliver nothing." >&2
    echo "!! Check the package (postgresql-$VERSION-wal2json) and, on 17.11+," >&2
    echo "!! that output_plugin_libraries names it alongside the rest." >&2
    exit 1
  fi
done

echo
echo "cluster $VERSION/$CLUSTER is up on port $PORT"
sudo -u postgres psql -p "$PORT" -At -c \
  "SELECT name || ' = ' || setting FROM pg_settings
    WHERE name IN ('wal_level','max_replication_slots','max_wal_senders','max_slot_wal_keep_size',
                   'output_plugin_libraries')
    ORDER BY name" | sed 's/^/  /'
echo
echo "export MALUDB_REALTIME_NODE_DSN=\"postgresql://postgres:$SUPERUSER_PW@127.0.0.1:$PORT/postgres\""
echo "export MALUDB_REALTIME_DB_HOST=$DATA_ADDRESS"
echo "export MALUDB_REALTIME_DB_PORT=$PORT"
echo
echo "drop it with: $0 --drop"
