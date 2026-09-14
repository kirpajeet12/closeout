#!/usr/bin/env bash
# First time only: prepare the server, place its private settings, ship the code. Later deploys: deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/.deploy
SSH="ssh -i $CLOSEOUT_KEY -o StrictHostKeyChecking=accept-new"
echo "1/3 preparing the server (installs Docker, ~2 minutes)"
$SSH "$CLOSEOUT_SERVER" 'bash -s' < deploy/server-setup.sh | tail -1
echo "2/3 placing the private settings"
scp -q -i "$CLOSEOUT_KEY" deploy/.env "$CLOSEOUT_SERVER:/srv/closeout/app/deploy/.env"
echo "3/3 shipping the code and starting the site (~3 minutes)"
deploy/deploy.sh
echo "open https://closeout.getcrewbrew.com"
