#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Make the voice clips in voice/ from charge_watch.SPOKEN, and voice/SOURCES (a sha256 and the source of each).

  pip install kokoro            # Kokoro-82M (Apache-2.0); downloads its model on first use
  python3 tools/make-voice.py   # rewrites voice/<kind>.wav for every spoken line, and voice/SOURCES
  python3 tools/make-voice.py --voice am_michael --speed 0.95

The clips shipped in voice/ were made this way with the voice am_puck, on a PC (not on the headset). They are
synthetic speech: no recording of anyone. Re-run after changing a line in SPOKEN; the tests check every line has
its clip and that SOURCES matches. Needs ffmpeg. Runs on the CPU (set CUDA_VISIBLE_DEVICES to use a GPU)."""
import argparse, hashlib, os, subprocess, sys, tempfile, wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import charge_watch as cw  # noqa: E402


def text_id(text):
    """A short hash of the spoken line, so the tests notice a line changed without its clip being remade."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--voice", default="am_puck", help="a Kokoro voice (default am_puck)")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--out", default=os.path.join(ROOT, "voice"))
    a = p.parse_args()
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    import numpy as np
    from kokoro import KPipeline
    pipe = KPipeline(lang_code=a.voice[0], repo_id="hexgrad/Kokoro-82M")
    os.makedirs(a.out, exist_ok=True)
    sources = []
    for kind, text in sorted(cw.SPOKEN.items()):
        audio = np.concatenate([np.asarray(r.audio) for r in pipe(text, voice=a.voice, speed=a.speed)])
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        path = os.path.join(a.out, kind + ".wav")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as t:
            raw = t.name
        with wave.open(raw, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
        # 16 kHz mono is plenty for speech and keeps the repo small
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", raw, "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", "-bitexact", "-map_metadata", "-1", path], check=True)
        os.unlink(raw)
        with open(path, "rb") as f:
            sources.append(f"{hashlib.sha256(f.read()).hexdigest()}  {kind}.wav  "
                           f"synthetic: Kokoro-82M (Apache-2.0), voice {a.voice}, speed {a.speed:g}, text {text_id(text)}")
        print(f"{os.path.getsize(path):>7}  {path}")
    with open(os.path.join(a.out, "SOURCES"), "w") as f:
        f.write("# Every clip here is synthetic speech made by tools/make-voice.py from the text in charge_watch.SPOKEN.\n"
                "# sha256  file  source\n" + "\n".join(sources) + "\n")


if __name__ == "__main__":
    main()
