#!/usr/bin/env python3
"""What the paid demo capture cost, from the usage the app records. Run with the app's Python:

    .venv/bin/python demo-video/cost.py
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("CLOSEOUT_DATA_DIR", str(HERE / "data"))
sys.path.insert(0, str(HERE.parent))

from closeout import pipeline, usage  # noqa: E402
from closeout.config import Settings  # noqa: E402
from closeout.drawings import cost_usd  # noqa: E402

s = usage.summary(pipeline.open_store(Settings()))
total = s["total_usd"]
for k, v in s["by_kind"].items():
    print(f"  {v['name']}: ${v['usd']:.2f} ({v['runs']} run{'s' if v['runs'] != 1 else ''})")
shots = HERE / "output" / "cap-ai" / "shots.json"
sug = json.loads(shots.read_text()).get("p07-suggested", {}) if shots.exists() else {}
if sug.get("usage"):
    usd = cost_usd(sug["usage"], sug.get("model_id") or "")
    total += usd
    print(f"  Write-up from the photo: ${usd:.2f}")
print(f"  Total: ${total:.2f}")
