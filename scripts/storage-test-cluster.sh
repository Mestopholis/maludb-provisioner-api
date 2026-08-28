#!/bin/bash
# Phase 10 slice 3 — stand up the object store the Storage tests need.
#
# SeaweedFS (ADR-055), on a **data address** rather than loopback. That is not a
# preference: ADR-035 forbids a rootless Podman container from reaching the
# node's loopback, measured in Phase 06 after a Realtime container turned out to
# reach a different cluster's PostgreSQL through `allow_host_loopback`. So
# `storage-api` addresses the object store the way it would address one in
# another datacentre, and it cannot address it any other way.
#
# That containment is also what makes ADR-055's "start on the existing hardware,
# separate later" cheap rather than merely intended: moving the bytes to a
# dedicated box is an endpoint change, because no code can have assumed
# co-location.
#
# Deliberately NOT idempotent-in-place: `--drop` first, then create. A store
# half-configured from a previous run is exactly the thing that makes a
# security assertion pass for the wrong reason -- the same rule
# `realtime-test-cluster.sh` follows.
#
# Safe only on a development host. It creates and destroys a whole object store
# and takes an interface up and down.
#
# It also prepares the node's PostgreSQL for the storage worker, which is part
# of the same job: the worker is a container with no route to loopback, so it
# reaches PostgreSQL at a data address, and `pg_hba.conf` has to admit that
# address or every tenant's Storage request fails authentication rather than
# authorization.
#
#   scripts/storage-test-cluster.sh              # build it, print the exports
#   scripts/storage-test-cluster.sh --drop       # destroy it
#
# Requires: passwordless sudo, curl, the `weed` binary (downloaded if absent).

set -euo pipefail

# Pinned, and one release line back from latest on purpose. Slice 0 ran its S3
# bake-off against 4.44 and noted that 4.44 had been published the same day it
# was tested; a store holding every customer's files should not track a release
# nobody has had time to find a fault in. 4.41 is three releases back and the
# bake-off was re-run against it -- `tests/test_object_store.py` is that re-run,
# so this pin and the evidence for it move together.
WEED_VERSION="${WEED_VERSION:-4.41}"
WEED_SHA256="${WEED_SHA256:-730f1ede19972c12954ee407b2d97679a2e4486d24fd987d371761ec395571b8}"
WEED_BIN="${WEED_BIN:-/usr/local/bin/weed}"

# Its own address on its own interface, beside the Realtime one at 10.90.0.1.
# Separate rather than shared: the two are reached by different containers for
# different reasons, and one address per purpose means a firewall rule or a
# `ss` line says which service it is about.
DATA_ADDRESS="${STORAGE_DATA_ADDRESS:-10.91.0.1}"
DATA_IFACE="${STORAGE_DATA_IFACE:-maludb-st}"
S3_PORT="${STORAGE_S3_PORT:-8333}"
MASTER_PORT="${STORAGE_MASTER_PORT:-9333}"
VOLUME_PORT="${STORAGE_VOLUME_PORT:-8080}"
FILER_PORT="${STORAGE_FILER_PORT:-8888}"

STATE_DIR="${STORAGE_STATE_DIR:-/var/lib/maludb-seaweedfs}"
RUN_DIR="${STORAGE_RUN_DIR:-/run/maludb-seaweedfs}"
BUCKET="${STORAGE_BUCKET:-maludb}"

# The platform's single bucket (ADR-057). One credential, held by the platform,
# never issued to a customer: tenancy for objects lives in the key prefix and
# the tenant database, not in the object store.
ACCESS_KEY="${STORAGE_ACCESS_KEY:-maludb-platform}"

PIDFILE="$RUN_DIR/weed.pid"

# Stopping it is worth a function rather than a `pkill` line, because the
# obvious `pkill -f "weed server .*-dir=$STATE_DIR"` does not match: the server
# is started through `sudo bash -c` with its arguments single-quoted, so the
# command line reads `-dir='/var/lib/...'` and the pattern misses.
#
# That is not a cosmetic bug and it is why this is written out. A missed stop
# left the previous server holding the port, the new one died with `address
# already in use`, and the readiness probe below was answered by the **old**
# process -- so the script reported success having started nothing. Its own
# header warns that a half-configured store is what makes an assertion pass for
# the wrong reason; this was that, in the script that says it.
stop_store() {
  if [ -f "$PIDFILE" ]; then
    sudo kill "$(sudo cat "$PIDFILE")" 2>/dev/null || true
    sleep 1
    sudo kill -9 "$(sudo cat "$PIDFILE")" 2>/dev/null || true
    sudo rm -f "$PIDFILE"
  fi
  # Fallback for a server started before this function existed, or by hand.
  # Matched on the argument-independent part and confirmed against /proc, so
  # quoting cannot make it miss.
  for pid in $(pgrep -f 'weed server' 2>/dev/null || true); do
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF "$STATE_DIR"; then
      sudo kill "$pid" 2>/dev/null || true
      sleep 1
      sudo kill -9 "$pid" 2>/dev/null || true
    fi
  done
}

PG_VERSION="${PG_VERSION:-17}"
PG_CLUSTER="${PG_CLUSTER:-main}"
HBA="/etc/postgresql/$PG_VERSION/$PG_CLUSTER/pg_hba.conf"
HBA_MARK="# maludb storage data address (Phase 10 slice 3)"

case "${1:-}" in
  --drop)
    if [ -f "$HBA" ]; then
      sudo sed -i "\|^$HBA_MARK\$|,+1d" "$HBA"
      sudo pg_ctlcluster "$PG_VERSION" "$PG_CLUSTER" reload 2>/dev/null || true
    fi
    # And the listening address, if this script is what added it. RESET rather
    # than a value: postgresql.auto.conf is ours to clear and the cluster's own
    # postgresql.conf is not ours to guess at.
    if sudo -u postgres psql -tAc "SHOW listen_addresses" 2>/dev/null \
         | grep -qF "$DATA_ADDRESS"; then
      sudo -u postgres psql -qc "ALTER SYSTEM RESET listen_addresses" >/dev/null
      sudo pg_ctlcluster "$PG_VERSION" "$PG_CLUSTER" restart 2>/dev/null || true
    fi
    stop_store
    sudo rm -rf "$STATE_DIR" "$RUN_DIR"
    sudo ip link del "$DATA_IFACE" 2>/dev/null || true
    echo "dropped the object store and the $DATA_IFACE interface"
    exit 0
    ;;
  "") ;;
  *) echo "usage: $0 [--drop]" >&2; exit 2 ;;
esac

# -- the binary ------------------------------------------------------------

if [ ! -x "$WEED_BIN" ]; then
  echo "== fetching SeaweedFS $WEED_VERSION =="
  TMP="$(mktemp -d)"
  curl -sSL --max-time 300 -o "$TMP/weed.tar.gz" \
    "https://github.com/seaweedfs/seaweedfs/releases/download/$WEED_VERSION/linux_amd64.tar.gz"
  # Checked rather than trusted. This binary holds every customer's files.
  echo "$WEED_SHA256  $TMP/weed.tar.gz" | sha256sum -c - >/dev/null
  tar xzf "$TMP/weed.tar.gz" -C "$TMP"
  sudo install -m 0755 "$TMP/weed" "$WEED_BIN"
  rm -rf "$TMP"
fi
"$WEED_BIN" version

# -- the data address ------------------------------------------------------

sudo ip link del "$DATA_IFACE" 2>/dev/null || true
sudo ip link add "$DATA_IFACE" type dummy
sudo ip addr add "$DATA_ADDRESS/32" dev "$DATA_IFACE"
sudo ip link set "$DATA_IFACE" up

# -- PostgreSQL, reachable from the container ------------------------------

# The worker connects from a network namespace with no route to this node's
# loopback (ADR-035), so it arrives at the data address and `pg_hba.conf` must
# admit it. Without this every Storage request fails as an authentication error
# -- which reads like a wrong password rather than an unprepared node.
#
# Added once and removed by --drop. Read with sudo: pg_hba.conf is 0640
# root:postgres, so an unprivileged grep does not report "absent", it fails --
# and `! grep` reads that failure as absent and appends the line again on every
# run.
if [ -f "$HBA" ]; then
  if ! sudo grep -qF "$HBA_MARK" "$HBA"; then
    sudo sed -i "1i $HBA_MARK\nhost    all    all    $DATA_ADDRESS/32    scram-sha-256" "$HBA"
    sudo pg_ctlcluster "$PG_VERSION" "$PG_CLUSTER" reload
  fi
  echo "== pg_hba.conf admits $DATA_ADDRESS =="
else
  echo "!! $HBA not found; set PG_VERSION/PG_CLUSTER if this node differs" >&2
fi

# Admitting the address is only half of it: the postmaster has to be listening
# on it. This was assumed rather than arranged for one release, because a
# development node configured by hand listens on `*` and CI's does not --
# `pg_createcluster` defaults to `localhost`. The failure that produced is worth
# naming, because nothing in it says "address": the container connects, is
# refused, storage-api never migrates its multitenant database, and the suite
# reports `the storage worker never became ready` sixty seconds later.
#
# Additive, and that matters -- this is the node's real cluster, serving every
# other test's tenants. A node already on `*` is left alone rather than narrowed
# to two addresses. `listen_addresses` is postmaster context, so this is a
# restart; a reload accepts the setting and goes on listening where it was.
CURRENT_LISTEN="$(sudo -u postgres psql -tAc "SHOW listen_addresses" 2>/dev/null || true)"
# Spaces stripped for the comparison only. `listen_addresses = 'localhost, ::1'`
# is a normal way to have written it by hand, and a match that misses it adds
# an address the node already has.
case ",${CURRENT_LISTEN// /}," in
  *",*,"*|*",$DATA_ADDRESS,"*)
    echo "== PostgreSQL already listens on $DATA_ADDRESS ($CURRENT_LISTEN) ==" ;;
  *)
    if [ -z "$CURRENT_LISTEN" ]; then
      # Empty is a legal value and means "no TCP at all", so it cannot be
      # appended to; and this script cannot be the thing that decides a node
      # answering nobody should start answering somebody.
      echo "!! listen_addresses is empty; not changing it. The storage worker" >&2
      echo "!! cannot reach PostgreSQL at $DATA_ADDRESS until it is set." >&2
    else
      sudo -u postgres psql -qc \
        "ALTER SYSTEM SET listen_addresses = '$CURRENT_LISTEN,$DATA_ADDRESS'" >/dev/null
      sudo pg_ctlcluster "$PG_VERSION" "$PG_CLUSTER" restart
      echo "== PostgreSQL now listens on $CURRENT_LISTEN,$DATA_ADDRESS =="
    fi ;;
esac

# -- credentials -----------------------------------------------------------

# Stop before removing the data directory, not after. Removing it under a live
# server leaves a process serving from deleted inodes -- which answers requests
# for a while and then does not.
stop_store
sudo rm -rf "$STATE_DIR" "$RUN_DIR"
sudo mkdir -p "$STATE_DIR" "$RUN_DIR"

SECRET_KEY="$(openssl rand -hex 24)"

# `weed`'s S3 identities file. One identity, all actions, on the one platform
# bucket -- there is no per-tenant credential here and there must not be:
# ADR-057 puts tenancy in the metadata layer, and a per-tenant S3 identity would
# be a second tenancy model that could disagree with the first.
sudo tee "$STATE_DIR/s3.json" >/dev/null <<EOF
{
  "identities": [
    {
      "name": "$ACCESS_KEY",
      "credentials": [
        { "accessKey": "$ACCESS_KEY", "secretKey": "$SECRET_KEY" }
      ],
      "actions": ["Admin", "Read", "Write", "List", "Tagging"]
    }
  ]
}
EOF
sudo chmod 0600 "$STATE_DIR/s3.json"

# -- the server ------------------------------------------------------------

# Bound to the data address only. Not 0.0.0.0: the node this runs on has a real
# network interface, and an object store answering on it would be reachable by
# anything that can route to the node.
# The redirect has to happen inside the privileged shell. Written the obvious
# way -- `sudo setsid weed ... > "$RUN_DIR/weed.log"` -- the redirect is
# performed by the *calling* shell, which is unprivileged and cannot write to a
# root-owned run directory, so the server dies before it logs why.
sudo bash -c "setsid '$WEED_BIN' server \
  -dir='$STATE_DIR' \
  -ip='$DATA_ADDRESS' \
  -ip.bind='$DATA_ADDRESS' \
  -master.port='$MASTER_PORT' \
  -volume.port='$VOLUME_PORT' \
  -filer -filer.port='$FILER_PORT' \
  -s3 -s3.port='$S3_PORT' \
  -s3.config='$STATE_DIR/s3.json' \
  >'$RUN_DIR/weed.log' 2>&1 </dev/null &
  echo \$! > '$PIDFILE'"

sleep 1
WEED_PID="$(sudo cat "$PIDFILE" 2>/dev/null || echo 0)"

echo "== waiting for the S3 gateway =="
for _ in $(seq 1 60); do
  # The liveness check comes first and is the point. A port answering 403 says
  # *a* server is there, not that the one just started is -- which is exactly
  # how a failed start reported success once already.
  if ! sudo kill -0 "$WEED_PID" 2>/dev/null; then
    echo "the object store exited during startup; see $RUN_DIR/weed.log" >&2
    sudo tail -20 "$RUN_DIR/weed.log" >&2 || true
    exit 1
  fi
  if curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://$DATA_ADDRESS:$S3_PORT" \
       | grep -qE '^(200|403|404)$'; then
    break
  fi
  sleep 1
done

sudo kill -0 "$WEED_PID" 2>/dev/null || {
  echo "the object store is not running; see $RUN_DIR/weed.log" >&2
  sudo tail -20 "$RUN_DIR/weed.log" >&2 || true
  exit 1
}
curl -s --max-time 3 -o /dev/null -w 'S3 gateway answers %{http_code} (pid '"$WEED_PID"')\n' \
  "http://$DATA_ADDRESS:$S3_PORT"

# -- the platform bucket ---------------------------------------------------

# One bucket for the whole deployment (ADR-057). Created here rather than by the
# worker: `storage-api` assumes its bucket exists and reports a missing one as a
# generic failure on the first upload, which is a confusing way to learn that
# node preparation was incomplete.
echo "== creating the platform bucket =="
"$WEED_BIN" shell -master="$DATA_ADDRESS:$MASTER_PORT" <<EOF >/dev/null 2>&1 || true
s3.bucket.create -name $BUCKET
EOF
"$WEED_BIN" shell -master="$DATA_ADDRESS:$MASTER_PORT" <<EOF 2>/dev/null | grep -q "$BUCKET" \
  && echo "   bucket $BUCKET present" || echo "!! bucket $BUCKET was not created" >&2
s3.bucket.list
EOF

echo
echo "Object store ready. Export these:"
echo
echo "export MALUDB_STORAGE_S3_ENDPOINT=http://$DATA_ADDRESS:$S3_PORT"
echo "export MALUDB_STORAGE_S3_BUCKET=$BUCKET"
echo "export MALUDB_STORAGE_S3_ACCESS_KEY=$ACCESS_KEY"
echo "export MALUDB_STORAGE_S3_SECRET_KEY=$SECRET_KEY"
# Phase 11 slice 4 (ADR-069): where the replication factor is read from. The S3
# endpoint cannot answer "how many copies of this byte exist"; the master can,
# and without this the durability check reports the store as undeclared.
echo "export MALUDB_STORAGE_MASTER_ENDPOINT=http://$DATA_ADDRESS:$MASTER_PORT"
echo
echo "Never point these at a store holding customer data: --drop destroys it."
