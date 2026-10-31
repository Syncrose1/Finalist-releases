"""The voice of Patient and Examiner Claude: hears the candidate and speaks the replies.
One WebSocket per station, on 127.0.0.1 only (the node passes /v1/voice through to it, after its own
sign-in checks).
  in   binary   the microphone: 16 kHz mono int16, any frame size
       json     {"type": "hello", "patient": {"sex", "age"}}   picks the voices, answered with "ready"
                {"type": "speak", "id": n, "text": "...", "role": "patient"|"examiner"}
                {"type": "stop"}                                stop talking now (the candidate cut in)
  out  json     {"type": "loading"|"ready"|"speech_start"|"heard"|"spoken"|"error", ...}
                "ready" carries "rate", the sample rate of the audio that follows (default 24000)
       binary   4-byte little-endian speak id, then mono int16 audio at that rate

One engine for every machine (Raahat, 31 Oct 2026: "I would prefer one backend"): audio.cpp's
server, started here and stopped with this process. It speaks with Ming 3.0 (a GPU: Vulkan, or Metal
on a Mac) or Pocket TTS (a CPU), every voice cloned from the same fixed clips, and hears with Parakeet
TDT or Qwen3-ASR (below). Measured on an RTX 3060 over Vulkan: Ming streams its first audio in 0.3 s at 0.78x real time,
Pocket in 0.04 s; Parakeet transcribes 6.6 s in 0.08 s. On a Ryzen 5600G's CPU: Pocket 0.52x,
Parakeet ~10x real time. Turn-taking keeps Silero VAD per 32 ms frame (its ONNX model, no PyTorch).
The Python Ming this replaced (PyTorch, CUDA only) is in git history: 7d294b8.
Exits after --idle seconds with no station connected, so the GPU is only held while it's in use.
"""
import argparse
import asyncio
import atexit
import collections
import http.client
import io
import json
import os
import queue
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort
import websockets

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from speakable import speakable                     # our English normaliser (Ming's mangles English)
ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=47615)
ap.add_argument("--engine-port", type=int, default=47616)
ap.add_argument("--engine", required=True, help="folder holding audio.cpp's audiocpp_server")
ap.add_argument("--models", required=True, help="folder holding the GGUF models")
ap.add_argument("--voices", required=True, help="the station voices: voices.json, the clips, pocket/*.safetensors")
ap.add_argument("--mouth", choices=["ming", "pocket"], default="pocket")
ap.add_argument("--backend", default="cpu", help="vulkan, metal or cpu")
ap.add_argument("--device", type=int, default=0)
ap.add_argument("--ear", choices=["qwen", "parakeet"], default="parakeet")
ap.add_argument("--drills", default="", help="the node's drills.json: each station's terms steer the ear")
ap.add_argument("--threads", type=int, default=max(2, min(4, (os.cpu_count() or 4) // 2)))
ap.add_argument("--idle", type=int, default=600)
ARGS, _ = ap.parse_known_args()

MODELS = {
    "ming": {"family": "ming3_tts", "file": "ming-3.0-tts-0.5b-q8_0.gguf", "rate": 44100},
    "pocket": {"family": "pocket_tts", "file": "PocketTTS-GGUF/english/pocket-tts-english-q8_0.gguf", "rate": 24000},
    "parakeet": {"family": "parakeet_tdt", "file": "Parakeet-TDT-0.6B-v3-GGUF/parakeet-tdt-0.6b-v3-q8_0.gguf"},
    "qwen": {"family": "qwen3_asr", "file": "Qwen3-ASR-0.6B-GGUF/qwen3-asr-0.6b-q8_0.gguf"},
}
# The ear (31 Oct 2026, 32 synthetic clips of students' medical summaries, 92 key terms): Parakeet got 64%
# of the terms right; Qwen3-ASR 83%, and 95% given the station's terms as context, at 0.4 s an utterance
# on an RTX 3060 (Parakeet 0.07 s) but 4.3 s on a 5600G's CPU (Parakeet 0.56 s). So: Qwen3 with the
# station's terms on a graphics card, Parakeet on a CPU. Context can pull a word towards a listed term
# (haematemesis heard as haemoptysis once), so the listeners are still told it's speech recognition.
RATE = MODELS[ARGS.mouth]["rate"]
HOLD = int(0.12 * RATE)             # the newest audio waits for the next look, which can still cut it
SOUND_TAGS = {"[Sigh]", "[Cough]", "[Breathing]", "[Uhm]", "[Laughter]"}
# 28 Oct 2026: with a sound tag in the line, Ming 0.5B often doesn't stop when the words end. So tags
# are stripped, and every reply goes through `runaway`.
ALLOW_TAGS = False
LEXICON = json.loads((HERE / "lexicon.json").read_text()) if (HERE / "lexicon.json").exists() else {}

STATE = {"ready": False, "error": None, "conns": 0, "last": time.time()}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ---- the engine -----------------------------------------------------------------------------------

class Engine:
    """audio.cpp's server, configured for the station voices; it lives and dies with this process."""

    def __init__(self):
        vs = json.loads((Path(ARGS.voices) / "voices.json").read_text())
        self.voices = {v["id"]: v for v in vs}
        mouth = MODELS[ARGS.mouth]
        if ARGS.mouth == "ming":
            presets = {v["id"]: {"voice_ref": str(Path(ARGS.voices) / v["file"]), "reference_text": v["text"]} for v in vs}
        else:
            presets = {v["id"]: {"options": {"voice_embedding_file": str(Path(ARGS.voices) / "pocket" / f"{v['id']}.safetensors")}} for v in vs}
        cfg = {"host": "127.0.0.1", "port": ARGS.engine_port, "backend": ARGS.backend, "device": ARGS.device, "threads": ARGS.threads,
               "lazy_load": False, "max_loaded_models": 0, "models": [
                   {"id": "mouth", "family": mouth["family"], "path": str(Path(ARGS.models) / mouth["file"]), "task": "tts",
                    "mode": "streaming", "voice_presets": presets,
                    **({"load_options": {"language": "english"}} if ARGS.mouth == "pocket" else {})},
                   {"id": "ear", "family": MODELS[ARGS.ear]["family"], "path": str(Path(ARGS.models) / MODELS[ARGS.ear]["file"]),
                    "task": "asr", "mode": "offline"}]}
        conf = Path(ARGS.models) / "engine.json"
        conf.write_text(json.dumps(cfg, indent=1))
        exe = Path(ARGS.engine) / ("audiocpp_server.exe" if os.name == "nt" else "audiocpp_server")
        env = {**os.environ, "LD_LIBRARY_PATH": str(Path(ARGS.engine)) + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")}
        self.p = subprocess.Popen([str(exe), "--config", str(conf)], cwd=str(ARGS.engine), env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=open(Path(ARGS.models) / "engine.log", "w"))
        atexit.register(self.close)
        end = time.time() + 300                 # first load: models from disk, Vulkan's shaders
        while time.time() < end:
            if self.p.poll() is not None:
                raise RuntimeError(f"the voice engine stopped (see {Path(ARGS.models) / 'engine.log'})")
            try:
                if json.loads(self._get("/health")).get("status") == "ok":
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        raise RuntimeError("the voice engine didn't start in 5 minutes")

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()

    def _conn(self, timeout=120):
        return http.client.HTTPConnection("127.0.0.1", ARGS.engine_port, timeout=timeout)

    def _get(self, path):
        c = self._conn(2)
        c.request("GET", path)
        return c.getresponse().read()

    def speak(self, voice, text):
        """Streams one fragment: yields int16 numpy chunks; closing the generator stops the render."""
        c = self._conn()
        c.request("POST", "/v1/audio/speech", json.dumps({"model": "mouth", "input": text, "voice": voice,
                                                          "response_format": "pcm", "stream_format": "audio"}),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        if r.status != 200:
            raise RuntimeError(f"the voice engine said {r.status}: {r.read()[:200]!r}")
        odd = b""
        try:
            while True:
                b = r.read1(16384) if hasattr(r, "read1") else r.read(16384)
                if not b:
                    break
                b, odd = odd + b, b""
                if len(b) % 2:
                    b, odd = b[:-1], b[-1:]
                if b:
                    yield np.frombuffer(b, dtype="<i2")
        finally:
            c.close()

    def hear(self, pcm16, prompt=None):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm16.tobytes())
        bnd = uuid.uuid4().hex
        parts = [("model", b"ear")] + ([("language", b"en")] if ARGS.ear == "qwen" else []) + \
                ([("prompt", prompt.encode())] if prompt and ARGS.ear == "qwen" else [])
        body = b"".join(f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v + b"\r\n" for k, v in parts)
        body += f'--{bnd}\r\nContent-Disposition: form-data; name="file"; filename="u.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
        body += buf.getvalue() + f"\r\n--{bnd}--\r\n".encode()
        c = self._conn(60)
        c.request("POST", "/v1/audio/transcriptions", body, {"Content-Type": f"multipart/form-data; boundary={bnd}"})
        r = c.getresponse()
        out = json.loads(r.read() or b"{}")
        c.close()
        return (out.get("text") or "").strip()


ENGINE = None


# ---- what to say, and checks on what came out -----------------------------------------------------

def clean(text):
    """What a person would actually say: no *actions* or (stage directions), and hard words respelt the
    way they're said."""
    text = re.sub(r"\*[^*]*\*", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", lambda m: m.group(0) if ALLOW_TAGS and m.group(0) in SOUND_TAGS else " ", text)
    for word, say in LEXICON.items():
        text = re.sub(rf"\b{re.escape(word)}\b", say, text, flags=re.IGNORECASE)
    return speakable(re.sub(r"\s+", " ", text).strip())


def fragments(text, most=180):
    """Sentences, and long ones at a comma: each is one render (a failed one is retried on its own)."""
    out = []
    for s in re.split(r"(?<=[.!?…])\s+", text):
        while len(s) > most:
            cut = max(s.rfind(", ", 0, most), s.rfind("; ", 0, most))
            if cut < 40:
                break
            out.append(s[:cut + 1])
            s = s[cut + 2:]
        if s.strip():
            out.append(s.strip())
    return out


def spectrum(w, sr, n, hop):
    """|STFT| like torch.stft(center=True) with a Hann window: (bins, frames)."""
    x = w.astype(np.float32) / 32768 if w.dtype != np.float32 else w
    x = np.pad(x, n // 2, mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop]
    return np.abs(np.fft.rfft(frames * np.hanning(n + 1)[:-1].astype(np.float32), axis=1)).T


def runaway(w, sr):
    """Where generated speech has gone wrong, in samples, or None: 1.1 s of nothing after the words (the
    model failing to stop: silence, rumble, the start of a tone), or a tone held a second at levels speech
    never reaches. Calibrated on 48 Ming clips (28 Oct)."""
    n, hop = 2048, sr // 100
    if len(w) < n * 2:
        return None
    S = spectrum(w, sr, n, hop)
    f = np.fft.rfftfreq(n, 1 / sr)
    band = S[(f > 150) & (f < 4000)]
    tone = band.max(0) / (np.median(band, 0) + 1e-9)
    vdb = 20 * np.log10(S[(f > 300) & (f < 3500)].sum(0) + 1e-9)
    loud = vdb > vdb.max() - 32
    spoke = False
    for i in range(0, S.shape[1] - 50 + 1, 10):
        if loud[i:i + 50].mean() > 0.3:
            spoke = True
        if i + 100 <= S.shape[1] and np.median(tone[i:i + 100]) > 600:
            return i * hop
        if spoke and i + 110 <= S.shape[1] and not loud[i:i + 110].any():
            return (i + 25) * hop
    return None


def voiced_secs(w, sr):
    """Seconds at speech level in the voice band."""
    n = 2048
    if len(w) < n:
        return 0.0
    S = spectrum(w, sr, n, sr // 20)
    f = np.fft.rfftfreq(n, 1 / sr)
    return float((20 * np.log10(S[(f > 300) & (f < 3500)].sum(0) + 1e-9) > 0).sum()) * 0.05


def starts_as_speech(w, sr):
    """Good renders reach speech level within ~0.2 s; failed ones (Ming, on hard medical words) sit at a
    noise floor, then swell into a hum and never become speech."""
    return voiced_secs(w, sr) >= 0.15


class Retry(Exception):
    """A render that failed from its first moments: thrown away before anything was played."""


def synth(v, text, emit, cancelled):
    """Streams one reply in the voice v: emit(int16 numpy, final) as audio becomes safe to play."""
    text = clean(text)
    if not re.sub(r"\[[^\]]*\]", "", text).strip():
        return
    frags = fragments(text)
    for k, frag in enumerate(frags):
        for attempt in range(3):
            try:
                speak_fragment(v, frag, emit, cancelled, last_try=attempt == 2, last_frag=k == len(frags) - 1)
                break
            except Retry:
                log(f"retrying a failed render ({attempt + 1}): {frag[:60]!r}")
        if cancelled():
            return


def speak_fragment(v, frag, emit, cancelled, last_try, last_frag):
    words = len(re.findall(r"[A-Za-z']+", frag))
    got, sent, looked, lead = np.zeros(0, dtype=np.int16), 0, 0, True
    check = ARGS.mouth == "ming"            # Ming's way of failing (a hum from the start, or going quiet partway)
    stream = ENGINE.speak(v["id"], frag)
    try:
        for chunk in stream:
            if cancelled():
                return
            if lead:                                        # leading quiet (Pocket opens with ~0.4 s) is dropped
                loud = np.nonzero(np.abs(chunk.astype(np.int32)) > 300)[0]
                if not len(loud) and len(got) + len(chunk) < int(0.6 * RATE):
                    got = np.concatenate([got, chunk])
                    continue
                start = max(0, len(got) + (loud[0] if len(loud) else 0) - int(0.03 * RATE))
                chunk, got, lead = np.concatenate([got, chunk])[start:], np.zeros(0, dtype=np.int16), False
            got = np.concatenate([got, chunk])
            if check and sent == 0 and len(got) < int(0.5 * RATE):
                continue                                    # the first half second decides whether it started
            if check and sent == 0 and not last_try and not starts_as_speech(got[: int(0.5 * RATE)], RATE):
                raise Retry()
            if len(got) - looked >= RATE // 2:              # a look for a runaway every half second of audio
                looked = len(got)
                cut = runaway(got, RATE)
                if cut is not None:
                    log(f"runaway cut at {cut / RATE:.1f}s: {frag[:60]!r}")
                    got = got[:cut]
                    break
            upto = max(0, len(got) - HOLD)
            if upto > sent:
                emit(got[sent:upto].copy(), False)
                sent = upto
        else:
            cut = runaway(got, RATE)
            if cut is not None and cut >= sent:
                got = got[:cut]
    finally:
        stream.close()
    if cancelled():
        return
    if check and sent == 0 and voiced_secs(got, RATE) < 0.15 * words:
        if not last_try:
            raise Retry()
        log(f"gave up on a render after 3 tries: {frag[:60]!r}")
        return
    loud = np.nonzero(np.abs(got[sent:].astype(np.int32)) > 300)[0]
    end = sent + (loud[-1] + int(0.08 * RATE) if len(loud) else 0)     # trailing silence trimmed
    tail = got[sent:min(len(got), end)].astype(np.float32)
    if len(tail):
        k = min(int(0.03 * RATE), len(tail))
        tail[-k:] *= np.linspace(1, 0, k, dtype=np.float32)
    if not last_frag:                                                    # a breath between sentences
        tail = np.concatenate([tail, np.zeros(int(0.12 * RATE), dtype=np.float32)])
    emit(tail.astype(np.int16), True)


JOBS = queue.Queue()
MOUTH_READY = threading.Event()


def mouth_worker():
    """One reply at a time, in order."""
    global ENGINE
    try:
        t = time.perf_counter()
        ENGINE = Engine()
        first = next(iter(ENGINE.voices.values()))
        synth(first, "Thank you. Please take a seat.", lambda *_: None, lambda: False)    # warm: nobody waits mid-station
        ENGINE.hear(np.zeros(16000, dtype=np.int16))
        log(f"voice ready in {time.perf_counter() - t:.1f}s: {ARGS.mouth} and {ARGS.ear} on {ARGS.backend}, {len(ENGINE.voices)} voices")
        MOUTH_READY.set()
    except Exception as e:
        log("the voice failed to start:", traceback.format_exc())
        STATE["error"] = f"The voice couldn't start: {e}"
        return
    while True:
        job = JOBS.get()
        try:
            synth(job["voice"], job["text"], job["emit"], lambda: job["conn"].stopped > job["at"])
        except Exception as e:
            log("synth failed:", traceback.format_exc())
            job["done"](str(e) or type(e).__name__)
            continue
        job["done"](None)


# ---- the ear --------------------------------------------------------------------------------------

class Vad:
    """Silero VAD from its ONNX file, the way silero_vad's own OnnxWrapper runs it: 512 samples at
    16 kHz with 64 samples of context, and its recurrent state carried between frames."""
    SESSION = None

    def __init__(self):
        if Vad.SESSION is None:
            o = ort.SessionOptions()
            o.inter_op_num_threads = o.intra_op_num_threads = 1
            Vad.SESSION = ort.InferenceSession(str(HERE / "silero_vad.onnx"), sess_options=o, providers=["CPUExecutionProvider"])
        self.reset_states()

    def reset_states(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def __call__(self, frame):
        x = np.concatenate([self.context, frame.reshape(1, -1)], axis=1)
        out, self.state = Vad.SESSION.run(None, {"input": x, "state": self.state, "sr": np.array(16000, dtype=np.int64)})
        self.context = x[:, -64:]
        return float(out[0][0])


def station_terms(sid):
    """A station's own terms (its drill items' answers), to steer the ear."""
    try:
        d = json.loads(Path(ARGS.drills).read_text()).get(sid) or {}
    except (OSError, ValueError):
        return []
    out = []
    for g in d.get("groups", []):
        for it in g.get("items", []):
            a = (it.get("answer") or "").strip()
            if a and a not in out:
                out.append(a)
    return out[:40]


def has_speech(pcm, secs=0.25):
    """At least `secs` of speech by Silero, across the whole utterance."""
    v, n, need = Vad(), 0, int(secs * 16000 / 512)
    for i in range(0, len(pcm) - 511, 512):
        if v(pcm[i:i + 512].astype(np.float32) / 32768) > 0.5:
            n += 1
            if n >= need:
                return True
    return False


def echoed(text, prompt):
    """Qwen3-ASR, given silence or hiss with a context prompt, returns the prompt itself (31 Oct 2026: a
    whole station's turns arrived as "A medical conversation... Terms: Pain through to the back; ...").
    Any of the prompt's own phrases in the transcript means it's an echo."""
    if not prompt or not text:
        return False
    t = text.lower()
    if "medical conversation" in t or "scientific terminology" in t:
        return True
    terms = [x.strip().lower() for x in prompt.split("Terms:", 1)[-1].split(";") if len(x.strip()) > 3]
    return sum(1 for x in terms if x.rstrip(".") in t) >= 3


def keep_echo(pcm, text):
    """The audio behind an echo, kept on this machine (the last ten) so it can be tested against: 31 Oct, a
    Bluetooth headset's presses echoed every time, and no recording here reproduced it."""
    try:
        d = Path(ARGS.models).parent / "echoes"
        d.mkdir(exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S")
        with wave.open(str(d / f"{name}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm.tobytes())
        (d / f"{name}.txt").write_text(text)
        for old in sorted(d.glob("*.wav"))[:-10]:
            old.unlink(missing_ok=True)
            old.with_suffix(".txt").unlink(missing_ok=True)
    except OSError:
        pass


def hear(pcm, cb, prompt=None):
    """Transcribes off the socket's thread; cb({"text"}) when done. Nothing that isn't speech reaches the
    ear (with a prompt, Qwen3 turns silence into the prompt), and an echo of the prompt is dropped."""
    def go():
        try:
            if not has_speech(pcm):
                return cb({"text": "", "why": "no speech"})
            text = ENGINE.hear(pcm, prompt)
            if echoed(text, prompt):
                keep_echo(pcm, text)
                log("dropped an echo of the ear's prompt (the audio is kept in voice/echoes)")
                text = ENGINE.hear(pcm, None)
                text = "" if echoed(text, prompt) else text
            cb({"text": text})
        except Exception as e:
            log("hearing failed:", e)
            cb({"text": ""})
    threading.Thread(target=go, daemon=True).start()


# ---- one station ----------------------------------------------------------------------------------

FILLERS = {"um", "uh", "er", "erm", "so", "and", "but", "or", "because", "like", "the", "a", "to", "of", "with", "if", "then"}


def finished(text):
    """A transcript that reads as a finished turn: a sentence or a question, not a trailing 'and' or 'um'."""
    t = text.strip()
    words = re.findall(r"[A-Za-z']+", t.lower())
    return len(words) >= 3 and t[-1:] in ".?!" and words[-1] not in FILLERS


class Conn:
    START_P, END_P, HANG_MS = 0.5, 0.35, 550
    PEEK_MS = 250       # a first look at the words after this much quiet: a finished sentence ends the turn there

    def __init__(self, ws, loop):
        self.ws, self.loop = ws, loop
        self.vad = Vad()
        self.rest = np.zeros(0, dtype=np.int16)
        self.pre = collections.deque(maxlen=10)             # 320 ms before the speech was noticed
        self.buf, self.speaking, self.run, self.quiet = [], False, 0, 0
        self.talking_until = 0.0                             # when the audio sent so far finishes playing
        self.lock = threading.Lock()
        self.utt = 0                                         # which utterance (a peek's answer must still be about it)
        self.peeked, self.since_peek, self.peek_n = False, 0, 0
        self.ptt, self.holding = False, False      # push to talk: the button, not the silence, decides the turn
        self.stopped = 0.0
        self.voices = {}
        self.prompt = None                  # the station's terms, for the ear

    def send(self, obj):
        data = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj)
        asyncio.run_coroutine_threadsafe(self.ws.send(data), self.loop)

    def mic(self, data):
        """Frames in; utterances out. While we're talking, cutting in needs clearer speech (echo)."""
        if self.ptt:                                  # held: everything is the turn; released: nothing is
            if self.holding:
                with self.lock:
                    self.buf.append(np.frombuffer(data, dtype="<i2").copy())
            return
        a = np.concatenate([self.rest, np.frombuffer(data, dtype="<i2")])
        n = len(a) // 512
        self.rest = a[n * 512:]
        for i in range(n):
            f = a[i * 512:(i + 1) * 512]
            p = self.vad(f.astype(np.float32) / 32768)
            ours = time.monotonic() < self.talking_until
            if not self.speaking:
                self.pre.append(f)
                self.run = self.run + 1 if p > (0.8 if ours else self.START_P) else 0
                if self.run >= (4 if ours else 2):
                    self.speaking, self.buf, self.quiet, self.peeked = True, list(self.pre), 0, False
                    self.send({"type": "speech_start"})
            else:
                with self.lock:
                    if not self.speaking:                    # a peek ended the turn meanwhile
                        continue
                    self.buf.append(f)
                    self.quiet = self.quiet + 32 if p < self.END_P else 0
                    if p >= self.END_P:                     # talking again: a later pause gets its own look
                        self.since_peek += 1
                        self.peeked = False
                    if self.quiet >= self.PEEK_MS and not self.peeked:
                        self.peek()
                    if self.quiet >= self.HANG_MS or len(self.buf) > 30 * 16000 // 512:
                        self.end()

    def peek(self):
        """Transcribes what's been said so far, while the quiet continues. If it reads as a finished
        sentence and nothing more has been said by the time it's back, that's the turn: 200-300 ms
        sooner than waiting out the whole silence."""
        self.peeked, self.since_peek = True, 0
        self.peek_n += 1
        n, utt, pcm, t = self.peek_n, self.utt, np.concatenate(self.buf), time.perf_counter()

        def back(d):
            text = (d.get("text") or "").strip()
            with self.lock:
                if self.utt != utt or self.peek_n != n or not self.speaking or self.since_peek or not finished(text):
                    return
                self.speaking, self.buf, self.run = False, [], 0
                self.utt += 1
                self.vad.reset_states()
            self.send({"type": "heard", "text": text, "ms": round(1000 * (time.perf_counter() - t)), "early": True})
        hear(pcm, back, self.prompt)

    def end(self):
        pcm = np.concatenate(self.buf)
        self.speaking, self.buf, self.run = False, [], 0
        self.utt += 1
        self.peeked = False
        self.vad.reset_states()
        if len(pcm) < 0.25 * 16000:
            self.send({"type": "heard", "text": ""})
            return
        t = time.perf_counter()
        hear(pcm, lambda d: self.send({"type": "heard", "text": d.get("text", ""), "ms": round(1000 * (time.perf_counter() - t))}), self.prompt)

    def speak(self, m):
        v = ENGINE.voices.get(self.voices.get(m.get("role", "patient")))
        if not v:
            return self.send({"type": "error", "text": "no voice for " + str(m.get("role"))})
        sid = int(m.get("id") or 0)

        def emit(pcm, final):
            now = time.monotonic()
            self.talking_until = max(self.talking_until, now) + len(pcm) / RATE
            if len(pcm):
                self.send(struct.pack("<I", sid) + pcm.tobytes())
        JOBS.put({"voice": v, "text": m.get("text") or "", "emit": emit, "conn": self, "at": time.monotonic(),
                  "done": lambda err: self.send({"type": "spoken", "id": sid, **({"error": err} if err else {})})})

    def pick(self, patient):
        """A patient voice of the right sex, at random, so stations don't all sound alike; the examiner's
        own steady voice, never used for a patient."""
        import random
        sex = (patient or {}).get("sex")
        vs = ENGINE.voices.values()
        pats = [v for v in vs if v.get("role") == "patient"] or list(vs)
        p = random.choice([v for v in pats if v.get("sex") == sex] or pats)
        ex = next((v for v in vs if v.get("role") == "examiner"), p)
        self.voices = {"patient": p["id"], "examiner": ex["id"]}
        return self.voices


async def handle(ws):
    STATE["conns"] += 1
    STATE["last"] = time.time()
    c = Conn(ws, asyncio.get_running_loop())
    try:
        if not STATE["ready"]:
            await ws.send(json.dumps({"type": "loading"}))
        async for msg in ws:
            STATE["last"] = time.time()
            if isinstance(msg, bytes):
                if STATE["ready"]:
                    await asyncio.to_thread(c.mic, msg)
                continue
            m = json.loads(msg)
            if m.get("type") == "hello":
                while not STATE["ready"] and not STATE["error"]:
                    await asyncio.sleep(0.2)
                if STATE["error"]:
                    await ws.send(json.dumps({"type": "error", "text": STATE["error"]}))
                    continue
                if "hang" in m:
                    c.HANG_MS = max(250, min(1500, int(m["hang"])))
                c.ptt = m.get("mode") == "ptt"
                terms = station_terms(m.get("station") or "")
                # the specific spellings carry it (92-term test: none 83%, a generic medical sentence 85%, the
                # terms 95%); the sentence frames them, and covers a station with no list
                c.prompt = "A medical conversation that contains medical and scientific terminology." + \
                    (" Terms: " + "; ".join(terms) + "." if terms else "")
                await ws.send(json.dumps({"type": "ready", "rate": RATE, "voices": c.pick(m.get("patient"))}))
            elif m.get("type") == "speak":
                c.speak(m)
            elif m.get("type") == "ptt_start":
                with c.lock:
                    c.holding, c.speaking, c.buf = True, True, []
            elif m.get("type") == "ptt_end":
                with c.lock:
                    c.holding = False
                    if c.speaking and c.buf:
                        c.end()
                    else:
                        c.speaking = False
                        await ws.send(json.dumps({"type": "heard", "text": ""}))
            elif m.get("type") == "stop":
                c.stopped = time.monotonic()
                c.talking_until = 0.0
    except websockets.ConnectionClosed:
        pass
    finally:
        c.stopped = time.monotonic()
        STATE["conns"] -= 1
        STATE["last"] = time.time()


def boot():
    try:
        while not MOUTH_READY.wait(1):
            if STATE["error"]:
                return
        STATE["ready"] = True
        log("ready")
    except Exception as e:
        STATE["error"] = f"The voice couldn't start: {e}"
        log(STATE["error"])


def quit_(*_):
    if ENGINE:
        ENGINE.close()
    os._exit(0)


async def main():
    threading.Thread(target=boot, daemon=True).start()
    threading.Thread(target=mouth_worker, daemon=True).start()
    async with websockets.serve(handle, "127.0.0.1", ARGS.port, max_size=2 ** 22):
        log("listening on", ARGS.port)
        while True:
            await asyncio.sleep(30)
            if STATE["conns"] == 0 and time.time() - STATE["last"] > ARGS.idle:
                log("idle; exiting")
                quit_()


if __name__ == "__main__":
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, quit_)
    asyncio.run(main())
