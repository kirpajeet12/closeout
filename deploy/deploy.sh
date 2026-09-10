#!/usr/bin/env bash
# Ship the current code to the live server and restart it. Records on the server are untouched.
# Usage: deploy/deploy.sh            (needs CLOSEOUT_SERVER=ubuntu@<ip> and CLOSEOUT_KEY=<pem> in deploy/.deploy)
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/.deploy
rsync -az --delete -e "ssh -i $CLOSEOUT_KEY -o StrictHostKeyChecking=accept-new" \
  --exclude .venv --exclude data --exclude inbox --exclude ios --exclude demo-video --exclude '__pycache__' --exclude .git \
  --exclude 'deploy/.env' --exclude 'deploy/.deploy' ./ "$CLOSEOUT_SERVER:/srv/closeout/app/"
ssh -i "$CLOSEOUT_KEY" "$CLOSEOUT_SERVER" 'cd /srv/closeout/app/deploy && sudo docker compose up -d --build --remove-orphans && sudo docker image prune -f >/dev/null && sleep 3 && curl -s -o /dev/null -w "app answers: %{http_code}\n" http://127.0.0.1:80/'
