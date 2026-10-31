"""The ear: Parakeet Redux on the CPU, one utterance at a time over stdin/stdout.

Run by voiced.py with a Python that has `moondream` installed (it needs a newer torch than the voice,
so it lives in its own environment). In: one JSON line per utterance, {"id", "pcm"} with pcm as base64
16 kHz mono int16. Out: {"ready": true} once warm, then {"id", "text", "ms"} per utterance.
"""
import base64
import json
import sys
import time

import numpy as np
import moondream as md

photon = md.photon("moondream/parakeet-redux", device="cpu")
ear = photon.__enter__()
ear.transcribe(audio=np.zeros(16000, dtype="float32"), sample_rate=16000)      # warm
print(json.dumps({"ready": True}), flush=True)

for line in sys.stdin:
    try:
        job = json.loads(line)
        audio = np.frombuffer(base64.b64decode(job["pcm"]), dtype="<i2").astype("float32") / 32768
        t = time.perf_counter()
        text = (ear.transcribe(audio=audio, sample_rate=16000).get("text") or "").strip()
        print(json.dumps({"id": job.get("id"), "text": text, "ms": round(1000 * (time.perf_counter() - t))}), flush=True)
    except Exception as e:                      # one bad utterance mustn't take the ear down
        print(json.dumps({"id": (job or {}).get("id") if isinstance(job, dict) else None, "error": str(e)}), flush=True)
