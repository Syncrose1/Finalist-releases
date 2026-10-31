"""The voice of Patient and Examiner Claude: hears the candidate and speaks the replies.

One WebSocket per station, on 127.0.0.1 only (revise.py passes /voice through to it, after its
own sign-in checks, so the tailnet and a friend's link both reach it at the page's own address).

  in   binary   the microphone: 16 kHz mono int16, any frame size
       json     {"type": "hello", "patient": {"sex", "age"}}   picks the voices, answered with "ready"
                {"type": "speak", "id": n, "text": "...", "role": "patient"|"examiner"}
                {"type": "stop"}                                stop talking now (the candidate cut in)
  out  json     {"type": "loading"|"ready"|"speech_start"|"heard"|"spoken"|"error", ...}
       binary   4-byte little-endian speak id, then 24 kHz mono int16 audio

The ear is Silero VAD (here, per connection) and Parakeet Redux (ear_worker.py, its own process and
Python). The mouth is Ming-3.0-tts 0.5B on the GPU: its flow head compiled (RTF 1.1 -> 0.44 on an
RTX 3060, first audio ~220 ms), every voice cloned from a fixed reference clip (a voice described
afresh, or even a named built-in, comes out a different person each time), 10 flow steps (3 drifts
underwater over a sentence). Measurements: RevisionSystem CLAUDE.md, 28 Oct 2026.

Exits after --idle seconds with no station connected, so the GPU is only held while it's in use.
"""
import argparse
import asyncio
import base64
import collections
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import torch
import torchaudio
import websockets

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from speakable import speakable                     # our English normaliser; Ming's mangles English
ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=47615)
ap.add_argument("--ming", default=str(Path.home() / "Ming-3.0-tts"))
ap.add_argument("--model", default=str(Path.home() / "models/Ming-3.0-tts-0.5b"))
ap.add_argument("--voices", default=str(Path.home() / ".local/share/revise/voices"))
ap.add_argument("--ear-python", default=str(Path.home() / "POLARIS/services/ear/.venv/bin/python"))
ap.add_argument("--idle", type=int, default=600)
ap.add_argument("--steps", type=int, default=10)
ARGS = ap.parse_args()

OUT_RATE = 24000
FIRST, EVERY = 6, 6                 # decode after 6 tokens (~0.5 s of speech), then every 6
HOLD = int(0.12 * OUT_RATE)         # the newest audio waits for the next decode, which can still revise it
SOUND_TAGS = {"[Sigh]", "[Cough]", "[Breathing]", "[Uhm]", "[Laughter]", "[Surprise-ah]", "[Surprise-oh]",
              "[Dissatisfaction-hnn]", "[Confirmation-en]", "[Question-ei]", "[Question-ah]", "[Question-en]",
              "[Question-oh]", "[Question-huh]"}
# 28 Oct 2026: with a sound tag in the line, Ming 0.5B often doesn't stop when the words end (same line,
# ten voices: no tag, 3.9-4.8 s every time; a [Sigh] at the start or middle, runaways to 20 s that turn
# into a rising tone or a sub-bass rumble). So tags are stripped, and every reply goes through `runaway`.
ALLOW_TAGS = False
LEXICON = json.loads((HERE / "lexicon.json").read_text()) if (HERE / "lexicon.json").exists() else {}

STATE = {"ready": False, "error": None, "conns": 0, "last": time.time()}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ---- the mouth ----------------------------------------------------------------------------------

sys.path.insert(0, ARGS.ming)
os.environ.setdefault("TQDM_DISABLE", "1")          # Ming draws a progress bar per sentence
try:
    from loguru import logger as _ming_log          # and logs every prompt at INFO
    _ming_log.remove()
    _ming_log.add(sys.stderr, level="WARNING")
except ImportError:
    pass
TTS = MODEL = None
VOICES = {}


def load_mouth():
    global TTS, MODEL
    import fm.flowloss as FL
    from ming_tts import MingTTS
    steps = ARGS.steps
    orig = FL.FlowLoss.sample
    FL.FlowLoss.sample = lambda self, z, lh, *a, **kw: orig(self, z, lh, *a, **{**kw, "steps": steps})
    t = time.perf_counter()
    TTS = MingTTS(ARGS.model, device="cuda:0")
    TTS.text_normalization = False                  # speakable() does it ("There's" became "T here is")
    MODEL = TTS.model
    dit = MODEL.flowloss.cfm.model
    dit.forward = torch.compile(dit.forward, mode="reduce-overhead", dynamic=False)
    lib = json.loads((Path(ARGS.voices) / "voices.json").read_text())
    for v in lib:
        latent, spk = TTS.prepare_prompt(str(Path(ARGS.voices) / v["file"]), True)
        VOICES[v["id"]] = {**v, "path": str(Path(ARGS.voices) / v["file"]), "latent": latent, "spk": spk}
    # compiling and warming cost ~15 s once; nobody should wait for it mid-station
    for v in list(VOICES.values())[:1]:
        synth(v, "Thank you. Please take a seat.", lambda *_: None, lambda: False)
    log(f"mouth ready in {time.perf_counter() - t:.1f}s with {len(VOICES)} voices")


def clean(text):
    """What a person would actually say: no *actions* or (stage directions), only sound tags Ming performs,
    and hard words respelt the way they're said."""
    text = re.sub(r"\*[^*]*\*", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", lambda m: m.group(0) if ALLOW_TAGS and m.group(0) in SOUND_TAGS else " ", text)
    for word, say in LEXICON.items():
        text = re.sub(rf"\b{re.escape(word)}\b", say, text, flags=re.IGNORECASE)
    return speakable(re.sub(r"\s+", " ", text).strip())


def runaway(w, sr):
    """Where generated speech has gone wrong, in samples, or None: 1.1 s of nothing after the words (the
    model failing to stop: silence, rumble, the start of a tone), or a tone held a second at levels speech
    never reaches. Calibrated on 48 clips (28 Oct): every runaway cut just after its last word, every
    clean clip left alone."""
    n, hop = 2048, sr // 100
    if len(w) < n * 2:
        return None
    S = torch.stft(torch.as_tensor(w, dtype=torch.float32).cpu(), n, hop, window=torch.hann_window(n), return_complex=True).abs().numpy()
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


class Retry(Exception):
    """A render that failed from its first moments: thrown away before anything was played."""


def speech_secs(w, sr):
    """Seconds of actual voice in a render (frames at speech level in the voice band)."""
    n = 2048
    if len(w) < n:
        return 0.0
    S = torch.stft(torch.as_tensor(w, dtype=torch.float32).cpu(), n, sr // 20, window=torch.hann_window(n), return_complex=True).abs().numpy()
    f = np.fft.rfftfreq(n, 1 / sr)
    return float((20 * np.log10(S[(f > 300) & (f < 3500)].sum(0) + 1e-9) > 0).sum()) * 0.05


def starts_as_speech(w, sr):
    """Good renders reach speech level within ~0.2 s. Failed ones (about 2 in 5 on hard medical words,
    28 Oct, at random: the same text usually works next time) sit at a noise floor for a second or two,
    then swell into a hum and never become speech."""
    n = 2048
    if len(w) < n:
        return False
    S = torch.stft(torch.as_tensor(w, dtype=torch.float32).cpu(), n, sr // 20, window=torch.hann_window(n), return_complex=True).abs().numpy()
    f = np.fft.rfftfreq(n, 1 / sr)
    lvl = 20 * np.log10(S[(f > 300) & (f < 3500)].sum(0) + 1e-9)
    return int((lvl > 0).sum()) >= 3     # 0.15 s of voice: clean renders peak at 50-60 early, failures sit at about -14


def synth(v, text, emit, cancelled):
    """Streams one reply in the voice v: emit(int16 numpy, final) as audio becomes safe to play."""
    text = clean(text)
    if not re.sub(r"\[[^\]]*\]", "", text).strip():
        return
    for frag in TTS.prepare_text(text, "TTS", "English", 24):
        for attempt in range(3):
            try:
                speak_fragment(v, frag, emit, cancelled, last_try=attempt == 2)
                break
            except Retry:
                log(f"retrying a failed render ({attempt + 1}): {frag[:60]!r}")
        if cancelled():
            return


def speak_fragment(v, frag, emit, cancelled, last_try):
    """One fragment, streamed; raises Retry if it fails from the start (nothing has been played yet)."""
    import tts_tasks
    from ming_tts import trim_trailing_silence
    req = tts_tasks.build_task_generation_kwargs("TTS", text=frag, language_id="English", prompt_wav_path=v["path"],
                                                 prompt_text=v["text"], use_spk_emb=True)
    toks, sent = [], 0

    def out(final):
        """Decodes what's generated so far and sends the new part; True when it has run away (stop)."""
        nonlocal sent
        sp = MODEL.audio.decode(torch.cat(toks, dim=1), past_key_values=None, use_cache=False, last_chunk=True)[0]
        return finish(sp.float().reshape(1, -1), final)

    @torch.autocast(device_type="cuda", enabled=False)     # the filters need float32, outside Ming's bf16
    def finish(sp, final):
        nonlocal sent
        w0 = sp[0].cpu().numpy()
        words = len(re.findall(r"[A-Za-z']+", frag))
        if sent == 0 and not last_try and not starts_as_speech(w0[: int(0.5 * TTS.sample_rate)], TTS.sample_rate):
            raise Retry()
        # the whole line, when it's done: too little voice for its words means it went quiet partway
        # (round 6, 28 Oct: lines that started with a click, then silence, got through a start-only check)
        if final and speech_secs(w0, TTS.sample_rate) < 0.15 * words:
            if not last_try:
                raise Retry()
            log(f"gave up on a render after 3 tries: {frag[:60]!r}")
            return True
        cut = runaway(sp[0].cpu().numpy(), TTS.sample_rate)
        if cut is not None:
            sp, final = sp[:, :cut], True
            log(f"runaway cut at {cut / TTS.sample_rate:.1f}s: {frag[:60]!r}")
        if final:
            sp = trim_trailing_silence(sp.cpu(), TTS.sample_rate).to(sp.device)
        sp = torchaudio.functional.highpass_biquad(sp, TTS.sample_rate, 70.0)    # no sub-bass pressure
        pcm = torchaudio.functional.resample(sp, TTS.sample_rate, OUT_RATE)[0]
        if final and pcm.shape[-1] > sent:
            k = min(int(0.03 * OUT_RATE), pcm.shape[-1] - sent)
            pcm[-k:] *= torch.linspace(1, 0, k, device=pcm.device)
        upto = pcm.shape[-1] if final else max(0, pcm.shape[-1] - HOLD)
        if upto > sent:
            emit((pcm[sent:upto].clamp(-1, 1) * 32767).short().cpu().numpy(), final)
            sent = upto
        return cut is not None
    with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16):
        for tok, _ in MODEL.sample(prompt=req["prompt"], text=frag, spk_emb=[e.clone() for e in v["spk"]],
                                   instruction=req["instruction"], prompt_latent=v["latent"], prompt_text=v["text"],
                                   max_decode_steps=min(600, int((1.0 + 0.45 * len(frag.split())) / 0.08)),   # ~0.45 s a word, +1 s
                                   cfg=req["cfg"], sigma=req["sigma"], temperature=req["temperature"],
                                   use_zero_spk_emb=False):
            if cancelled():
                return
            toks.append(tok)
            n = len(toks)
            if n >= FIRST and (n - FIRST) % EVERY == 0 and out(False):
                break
        else:
            if toks and not cancelled():
                out(True)

JOBS = queue.Queue()


def mouth_worker():
    """Everything on the GPU happens on this one thread: compiled CUDA graphs belong to the thread
    that recorded them, so loading, warming and every reply share it."""
    try:
        load_mouth()
        MOUTH_READY.set()
    except Exception as e:
        log("mouth failed:", traceback.format_exc())
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


MOUTH_READY = threading.Event()


# ---- the ear --------------------------------------------------------------------------------------

class Ear:
    def __init__(self):
        self.p = subprocess.Popen([ARGS.ear_python, str(HERE / "ear_worker.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.waiting, self.n, self.lock = {}, 0, threading.Lock()
        self.ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("ready"):
                self.ready.set()
                continue
            with self.lock:
                cb = self.waiting.pop(d.get("id"), None)
            if cb:
                cb(d)

    def hear(self, pcm16, cb):
        with self.lock:
            self.n += 1
            self.waiting[self.n] = cb
            n = self.n
        self.p.stdin.write(json.dumps({"id": n, "pcm": base64.b64encode(pcm16.tobytes()).decode()}) + "\n")
        self.p.stdin.flush()


EAR = None


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
        from silero_vad import load_silero_vad
        self.ws, self.loop = ws, loop
        self.vad = load_silero_vad()
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
            p = self.vad(torch.from_numpy(f.astype(np.float32) / 32768), 16000).item()
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
        sooner than waiting out the whole silence (the biggest cost in the loop, POLARIS measured)."""
        self.peeked, self.since_peek = True, 0
        self.peek_n += 1
        n, utt, pcm, t = self.peek_n, self.utt, np.concatenate(self.buf), time.perf_counter()

        def back(d):
            text = (d.get("text") or "").strip()
            with self.lock:
                if self.utt != utt or self.peek_n != n or not self.speaking or self.since_peek or not finished(text):
                    if os.environ.get("VOICE_DEBUG"):
                        log(f"peek declined: utt {self.utt != utt}, speaking {self.speaking}, since {self.since_peek}, finished {finished(text)}: {text!r}")
                    return
                self.speaking, self.buf, self.run = False, [], 0
                self.utt += 1
                self.vad.reset_states()
            self.send({"type": "heard", "text": text, "ms": round(1000 * (time.perf_counter() - t)), "early": True})
        EAR.hear(pcm, back)

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
        EAR.hear(pcm, lambda d: self.send({"type": "heard", "text": d.get("text", ""), "ms": round(1000 * (time.perf_counter() - t))}))

    def speak(self, m):
        v = VOICES.get(self.voices.get(m.get("role", "patient")))
        if not v:
            return self.send({"type": "error", "text": "no voice for " + str(m.get("role"))})
        sid = int(m.get("id") or 0)

        def emit(pcm, final):
            now = time.monotonic()
            self.talking_until = max(self.talking_until, now) + len(pcm) / OUT_RATE
            self.send(struct.pack("<I", sid) + pcm.tobytes())
        JOBS.put({"voice": v, "text": m.get("text") or "", "emit": emit, "conn": self, "at": time.monotonic(),
                  "done": lambda err: self.send({"type": "spoken", "id": sid, **({"error": err} if err else {})})})

    def pick(self, patient):
        """A patient voice of the right sex, at random, so stations don't all sound alike (age barely shows in
        these voices: the user, 28 Oct); the examiner's own steady voice, never used for a patient."""
        import random
        sex = (patient or {}).get("sex")
        pats = [v for v in VOICES.values() if v.get("role") == "patient"] or list(VOICES.values())
        p = random.choice([v for v in pats if v.get("sex") == sex] or pats)
        ex = next((v for v in VOICES.values() if v.get("role") == "examiner"), p)
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
                await ws.send(json.dumps({"type": "ready", "voices": c.pick(m.get("patient"))}))
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
    global EAR
    try:
        EAR = Ear()
        while not MOUTH_READY.wait(1):
            if STATE["error"]:
                return
        if not EAR.ready.wait(120):
            raise RuntimeError("the ear didn't start")
        STATE["ready"] = True
        log("ready")
    except Exception as e:
        STATE["error"] = f"The voice couldn't start: {e}"
        log(STATE["error"])


async def main():
    threading.Thread(target=boot, daemon=True).start()
    threading.Thread(target=mouth_worker, daemon=True).start()
    async with websockets.serve(handle, "127.0.0.1", ARGS.port, max_size=2 ** 22):
        log("listening on", ARGS.port)
        while True:
            await asyncio.sleep(30)
            if STATE["conns"] == 0 and time.time() - STATE["last"] > ARGS.idle:
                log("idle; exiting")
                os._exit(0)


if __name__ == "__main__":
    torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))
    asyncio.run(main())
