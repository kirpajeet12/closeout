#!/bin/bash
# Film the AI beats of the demo on the fictional Cedar Row project. This one costs money (well under a dollar):
# one write-up from a photo, one drafted message, one contractor photo filed, one chat question. Nothing else can run.
# Afterwards the demo server goes back to free mode.
cd "$(dirname "$0")/.." || exit 1
demo-video/serve.sh paid || exit 1
python3 demo-video/capture.py --paid; code=$?
demo-video/serve.sh > /dev/null
[ $code -eq 0 ] && echo "Done. Tell Claude the AI shots are filmed." || echo "Stopped early (see above). Tell Claude."
exit $code
