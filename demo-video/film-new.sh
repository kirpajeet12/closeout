#!/bin/bash
# Film the new-project beats: upload the fictional Alder Court zip and let Closeout file and read it.
# This one costs money (well under a dollar): it reads the 5 current sheets, writes the project summary and checks
# the folder once. It runs on its own scratch server (8778, demo-video/data-new), so the Cedar Row filming data is untouched.
cd "$(dirname "$0")/.." || exit 1
[ -f "demo-video/project2/Alder Court.zip" ] || python3 demo-video/make_project2.py || exit 1
DEMO_PORT=8778 DEMO_DATA=demo-video/data-new demo-video/serve.sh paid || exit 1
python3 demo-video/capture_new.py --paid; code=$?
for pid in $(lsof -tiTCP:8778 -sTCP:LISTEN); do kill "$pid" 2>/dev/null; done
[ $code -eq 0 ] && echo "Done. Tell Claude the new project is filmed." || echo "Stopped early (see above). Tell Claude."
exit $code
