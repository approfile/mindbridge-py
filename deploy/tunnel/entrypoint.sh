#!/bin/sh
set -eu
umask 077
# Windows bind mounts do not preserve 0600; copy into private container storage.
cp /identity/id_ed25519 /tmp/model-tunnel-key
cp /identity/known_hosts /tmp/model-known-hosts
chmod 600 /tmp/model-tunnel-key /tmp/model-known-hosts
exec ssh -N -T -i /tmp/model-tunnel-key \
  -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/tmp/model-known-hosts \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
  -L 0.0.0.0:8000:127.0.0.1:8000 -L 0.0.0.0:8001:127.0.0.1:8001 \
  -p "$SSH_PORT" "$SSH_USER@$SSH_HOST"
