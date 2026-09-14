#!/usr/bin/env python3
"""Voice the narrated cut: one ElevenLabs clip per scene -> audio/vo/<key>.mp3, plus <key>.json with when each character
is spoken, so scenes can cut on the word (media, not committed).

    python3 demo-video/gen_voice.py            # makes only clips whose line changed
    python3 demo-video/gen_voice.py --force    # makes every clip again

The key is read from the environment (ELEVENLABS_API_KEY) or the local jarvis config at run time; it is never printed or saved.
assemble.py imports VO so the film and the voice always use the same lines.
"""
import base64
import json
import os
import re
import ssl
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VO_DIR = HERE / "audio" / "vo"
VOICE = "EXAVITQu4vr4xnSDxMaL"      # Sarah, from the ElevenLabs library
MODEL = "eleven_multilingual_v2"

# one line per scene of the narrated cut, in film order
VO = {
    "title": "Closeout keeps a building project in one place: the drawings, the documents, the field reviews, and the contractor.",
    "upload": "Start a new project by uploading the project folder as one zip. Closeout reads every file, and files it by building and discipline.",
    "drawings": "Drawings land where they belong. The site plan and the site electrical under Site. The floor plans under the building.",
    "documents": "Documents get the same folders. The electrical set, the letter of assurance, and the building permit, each in its place.",
    "occupancy": "Documents needed before occupancy are kept as checklists. Here, one of eight letters of assurance is on file.",
    "folders": "Make folders of your own, and move files into them. Every move can be undone.",
    "overview": "Every project opens on its overview: how many items are ready to close, and what to do next.",
    "walk": "The field review is done on a phone. Take a photo, and tap where it is on the drawing.",
    "writeup": "Closeout writes up the deficiency. Check it, save it, and it is pinned to the plan.",
    "office": "In the office, every deficiency is listed with its unit, floor and sheet. The report shows each one with its photo and plan pin.",
    "contractor": "The contractor gets one link, with no account. They upload a photo, and Closeout files it on the item.",
    "decide": "Closeout checks the photo against the item. The engineer makes the call: ready to close, hold, or not accepted.",
    "ask": "And you can ask about the project. What does the contractor still need to send? Closeout lists the two items with nothing received.",
}


def api_key():
    if os.environ.get("ELEVENLABS_API_KEY"):
        return os.environ["ELEVENLABS_API_KEY"]
    m = re.search(r"sk_[A-Za-z0-9]{20,}", (Path.home() / "jarvis" / "jarvis.py").read_text())
    if not m:
        sys.exit("no ElevenLabs key found")
    return m.group(0)


def ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")


def main():
    VO_DIR.mkdir(parents=True, exist_ok=True)
    key, ctx, force = api_key(), ssl_ctx(), "--force" in sys.argv
    for name, text in VO.items():
        mp3, js = VO_DIR / f"{name}.mp3", VO_DIR / f"{name}.json"
        if not force and mp3.exists() and js.exists() and json.loads(js.read_text()).get("text") == text:
            continue
        body = json.dumps({"text": text, "model_id": MODEL,
                           "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}}).encode()
        req = urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}/with-timestamps?output_format=mp3_44100_128",
                                     data=body, headers={"xi-api-key": key, "Content-Type": "application/json"})
        out = json.load(urllib.request.urlopen(req, context=ctx, timeout=120))
        mp3.write_bytes(base64.b64decode(out["audio_base64"]))
        al = out["alignment"]
        js.write_text(json.dumps({"text": text, "chars": "".join(al["characters"]), "starts": al["character_start_times_seconds"]}))
        (VO_DIR / f"{name}.txt").unlink(missing_ok=True)
        print("voiced", name)


if __name__ == "__main__":
    main()
