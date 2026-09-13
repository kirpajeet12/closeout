#!/usr/bin/env python3
"""Score for the Closeout demo film (ElevenLabs music). Reads the key at run time; never prints it.

    python3 demo-video/gen_music.py        # writes demo-video/audio/score.mp3 once (the site walk)
    python3 demo-video/gen_music.py 2      # writes demo-video/audio/score2.mp3 once (the office and the contractor)
"""
import json
import os
import re
import ssl
import sys
import urllib.request
from pathlib import Path

TWO = sys.argv[1:] == ["2"]
OUT = Path(__file__).resolve().parent / "audio" / ("score2.mp3" if TWO else "score.mp3")
PROMPT = ("Instrumental product keynote score, no vocals. Opens quiet and spacious: a soft felt piano motif over a warm "
          "analog pad. Around 25 seconds a gentle pulsing synth arpeggio and light brushed percussion enter, confident "
          "and optimistic, building slowly. Mid section steady and focused, clean and modern, minimal. Around 115 seconds "
          "it lifts to a bright, uplifting full chord progression, then resolves softly to a single sustained piano chord "
          "at the end. Premium, calm, cinematic, like a hardware launch film.")
PROMPT2 = ("Instrumental product keynote score, no vocals, second movement. Starts with a clean, bright plucked synth "
           "motif and a tight modern beat at 110 bpm, warm bass, confident and forward moving from the first bar. Steady "
           "and focused through the middle with light rising strings. Around 70 seconds it opens into a wide, uplifting "
           "chord progression, then settles into a calm, resolved ending with a soft sustained chord and piano. Premium, "
           "optimistic, modern, like a software launch film.")


def key():
    src = (Path.home() / "jarvis/jarvis.py").read_text()
    m = re.search(r"""ELEVEN\w*KEY\w*\s*=\s*(?:os\.\w+\([^)]*,\s*)?["']([^"']{20,})["']""", src)
    return os.environ.get("ELEVENLABS_API_KEY") or (m and m.group(1))


def main():
    if OUT.exists():
        print("exists", OUT); return 0
    k = key()
    if not k:
        sys.exit("no ElevenLabs key found")
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request("https://api.elevenlabs.io/v1/music", method="POST",
                                 data=json.dumps({"prompt": PROMPT2 if TWO else PROMPT, "music_length_ms": 100000 if TWO else 152000}).encode(),
                                 headers={"xi-api-key": k, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=600) as r:
        data = r.read()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(data)
    print("score", len(data), "bytes ->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
