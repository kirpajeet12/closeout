#!/bin/bash
# Reset the fictional Cedar Row project and serve it on 127.0.0.1:8777 for filming.
# FREE mode (default): fake cloud credentials, so any model call fails at no cost.
# PAID mode (./serve.sh paid): your real AWS credentials, for the few steps Closeout writes itself.
cd "$(dirname "$0")/.." || exit 1
kill $(lsof -tiTCP:8777 -sTCP:LISTEN) 2>/dev/null; sleep 1
CLOSEOUT_DATA_DIR=demo-video/data .venv/bin/python demo-video/seed.py || exit 1
if [ "$1" = "paid" ]; then
  ( CLOSEOUT_DATA_DIR=demo-video/data nohup .venv/bin/uvicorn closeout.api:app --host 127.0.0.1 --port 8777 > /tmp/closeout-demo.log 2>&1 & )
else
  ( env AWS_ACCESS_KEY_ID=AKIAFAKEFAKEFAKE0000 AWS_SECRET_ACCESS_KEY=fake AWS_SESSION_TOKEN= AWS_PROFILE= AWS_CONFIG_FILE=/dev/null \
      AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true CLOSEOUT_DATA_DIR=demo-video/data \
      nohup .venv/bin/uvicorn closeout.api:app --host 127.0.0.1 --port 8777 > /tmp/closeout-demo.log 2>&1 & )
fi
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -o /dev/null http://127.0.0.1:8777/ && break; sleep 0.5; done
echo "demo server on http://127.0.0.1:8777 (${1:-free})"
