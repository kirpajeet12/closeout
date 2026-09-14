#!/usr/bin/env bash
# Put a copy of the office's real projects on the judges' copy, with the mailbox, Google keys and accounts taken out.
#   deploy/try-real.sh
# The live records are only read. The made-up projects are moved aside, not deleted. Prints counts, no settings.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/.deploy
SSH="ssh -i $CLOSEOUT_KEY -o StrictHostKeyChecking=accept-new $CLOSEOUT_SERVER"
scp -q -i "$CLOSEOUT_KEY" deploy/try-scrub.py "$CLOSEOUT_SERVER:/tmp/try-scrub.py"
$SSH 'set -e; cd /srv/closeout/app/deploy
sudo docker compose stop try
sudo mv /srv/closeout/try-data /srv/closeout/try-data-madeup-$(date +%Y%m%d-%H%M%S)
sudo mkdir -p /srv/closeout/try-data
sudo cp -a /srv/closeout/data/. /srv/closeout/try-data/
sudo rm -rf /srv/closeout/try-data/tls
sudo docker compose run --rm --no-deps -T -v /tmp/try-scrub.py:/scrub.py:ro try python /scrub.py
rm -f /tmp/try-scrub.py
sudo docker compose up -d try
sleep 3
echo "judges copy restarted"'
