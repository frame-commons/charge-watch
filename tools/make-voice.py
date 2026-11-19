#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Make the voice clips in voice/ from charge_watch.SPOKEN with espeak-ng (rule-based synthesis, offline).

  python3 tools/make-voice.py            # rewrites voice/<kind>.wav for every spoken line
Re-run after changing a line in SPOKEN; tests/test_charge_watch.py checks every line has its clip."""
import os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import charge_watch as cw  # noqa: E402

VOICE = ["-v", "en-us", "-s", "150", "-g", "3", "-a", "180"]   # US English, a little slower than default, loud
out = os.path.join(ROOT, "voice")
os.makedirs(out, exist_ok=True)
for kind, text in sorted(cw.SPOKEN.items()):
    path = os.path.join(out, kind + ".wav")
    raw = path + ".raw.wav"
    subprocess.run(["espeak-ng", *VOICE, "-w", raw, text], check=True)
    # 16 kHz mono is plenty for speech and keeps the repo small
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", raw, "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", "-bitexact", "-map_metadata", "-1", path], check=True)
    os.unlink(raw)
    print(f"{os.path.getsize(path):>7}  {path}")
