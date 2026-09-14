#!/usr/bin/env bash
# One time: put the judges' folder of made-up projects on the server and add the two try settings to the server's .env.
#   deploy/try-seed.sh path/to/try-data.tgz
# Refuses to touch a try-data folder that already has anything in it. Prints no settings.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/.deploy
SSH="ssh -i $CLOSEOUT_KEY -o StrictHostKeyChecking=accept-new $CLOSEOUT_SERVER"
[ -f "${1:-}" ] || { echo "give the path to try-data.tgz"; exit 1; }
for k in CLOSEOUT_TRY_ACCESS_CODE CLOSEOUT_TRY_HOST; do
  /usr/bin/grep -q "^$k=." deploy/.env || { echo "add $k to deploy/.env first"; exit 1; }
  $SSH "sudo grep -q '^$k=' /srv/closeout/app/deploy/.env" || /usr/bin/grep -h "^$k=" deploy/.env | $SSH "sudo tee -a /srv/closeout/app/deploy/.env >/dev/null"
done
echo "settings in place"
scp -q -i "$CLOSEOUT_KEY" "$1" "$CLOSEOUT_SERVER:/tmp/try-data.tgz"
$SSH 'sudo mkdir -p /srv/closeout/try-data && if [ -n "$(sudo ls -A /srv/closeout/try-data)" ]; then echo "try-data already has files; left as is"; else sudo tar -xzf /tmp/try-data.tgz -C /srv/closeout/try-data && echo "made-up projects in place"; fi; rm -f /tmp/try-data.tgz'
