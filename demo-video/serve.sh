#!/bin/bash
# Reset the fictional Cedar Row project and serve it on 127.0.0.1:8777 for filming.
# FREE mode (default): fake cloud credentials, so any model call fails at no cost.
# PAID mode (./serve.sh paid): your real AWS credentials, for the few steps Closeout writes itself.
cd "$(dirname "$0")/.." || exit 1
# DEMO_PORT / DEMO_DATA pick another port and scratch folder (the new-project film uses 8778 and demo-video/data-new).
PORT=${DEMO_PORT:-8777}; DATA=${DEMO_DATA:-demo-video/data}
# Stop the last demo server and wait until it has let go of the data folder before reseeding it.
for pid in $(lsof -tiTCP:$PORT -sTCP:LISTEN); do kill "$pid" 2>/dev/null; done
for i in $(seq 1 20); do lsof -tiTCP:$PORT -sTCP:LISTEN >/dev/null || break; sleep 0.5; done
for pid in $(lsof -tiTCP:$PORT -sTCP:LISTEN); do kill -9 "$pid" 2>/dev/null; done
for pid in $(lsof -t $DATA/closeout.db 2>/dev/null); do kill -9 "$pid" 2>/dev/null; done
sleep 0.5
CLOSEOUT_DATA_DIR=$DATA .venv/bin/python demo-video/seed.py || exit 1
if [ "$1" = "paid" ]; then
  ( CLOSEOUT_DATA_DIR=$DATA nohup .venv/bin/uvicorn closeout.api:app --host 127.0.0.1 --port $PORT > /tmp/closeout-demo-$PORT.log 2>&1 & )
else
  ( env AWS_ACCESS_KEY_ID=AKIAFAKEFAKEFAKE0000 AWS_SECRET_ACCESS_KEY=fake AWS_SESSION_TOKEN= AWS_PROFILE= AWS_CONFIG_FILE=/dev/null \
      AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true CLOSEOUT_DATA_DIR=$DATA \
      nohup .venv/bin/uvicorn closeout.api:app --host 127.0.0.1 --port $PORT > /tmp/closeout-demo-$PORT.log 2>&1 & )
fi
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -o /dev/null http://127.0.0.1:$PORT/ && break; sleep 0.5; done
echo "demo server on http://127.0.0.1:$PORT (${1:-free})"
