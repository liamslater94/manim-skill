#!/usr/bin/env python3
"""
Find the best voice-cloning sample in a long recording of the user.

Zero-shot cloning copies a short (~12 s) sample, so which 12 seconds you give it matters more than
how long you recorded. This splits a recording (e.g. a read-through of several short "takes") into
candidate clips at natural pauses, clones a test line from each, and scores every clone for how much
it sounds like the speaker (speaker-embedding similarity against the whole recording) and how
clearly it speaks (Whisper checks the test line). The best clips are written out with previews.

    python voice-pick.py my-recording.m4a --out-dir picks/ --say z=zed

Writes <out-dir>/results.json (best first) plus clip-NN.wav (the sample) and clip-NN-test.mp3 (the
clone saying the test line) for every candidate. Progress lines look like "PROGRESS 3/12".
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tts_generate", HERE / "tts-generate.py")
tts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tts)

SPEAKER_MODEL = "wespeaker_en_voxceleb_resnet34_LM.onnx"
TEST_LINE = ("So, take row two, and subtract three lots of row one. Now look at what happens to z: "
             "it cancels, which is exactly what we wanted.")
MIN_S, MAX_S = 5.0, 12.0


def speech_runs(audio, sr):
    """(start, end) sample ranges of speech, and the pause after each, from a simple level gate."""
    import numpy as np

    db, hop = tts.frame_db(audio, sr)
    floor, speech_level = np.percentile(db, 10), np.percentile(db, 90)
    quiet = db < max(floor + 0.35 * (speech_level - floor), speech_level - 28)   # adapts to room noise
    runs, start = [], None
    for i, q in enumerate(quiet):
        if not q and start is None:
            start = i
        elif q and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(quiet)])
    # merge speech separated by gaps under 0.25 s (inside words and phrases)
    merged = []
    for r in runs:
        if merged and (r[0] - merged[-1][1]) * hop / sr < 0.25:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    out = []
    for k, (a, b) in enumerate(merged):
        nxt = merged[k + 1][0] if k + 1 < len(merged) else len(quiet)
        prev = merged[k - 1][1] if k else 0
        out.append({"a": a * hop, "b": b * hop, "gap_before": (a - prev) * hop / sr,
                    "gap_after": (nxt - b) * hop / sr})
    return out


def candidates(audio, sr, limit):
    """Clips of MIN_S..MAX_S that start after a pause and end at one, longest pauses preferred."""
    runs = speech_runs(audio, sr)
    clips = []
    for i, r in enumerate(runs):
        if i and r["gap_before"] < 0.3:
            continue    # start at the beginning of a sentence or take, not mid-phrase
        best = None
        for j in range(i, len(runs)):
            a, b = runs[i]["a"], runs[j]["b"]
            length = (b - a) / sr
            if length > MAX_S:
                break
            if length >= MIN_S and runs[j]["gap_after"] >= 0.25:
                best = (a, b, runs[i]["gap_before"] + runs[j]["gap_after"])
        if best:
            clips.append(best)
    # drop clips that mostly overlap a better-bounded one
    chosen = []
    for a, b, gaps in sorted(clips, key=lambda c: -c[2]):
        if all(min(b, cb) - max(a, ca) < 0.3 * (b - a) for ca, cb, _ in chosen):
            chosen.append((a, b, gaps))
    chosen.sort()
    if len(chosen) > limit:   # spread the survivors across the recording
        step = len(chosen) / limit
        chosen = [chosen[int(k * step)] for k in range(limit)]
    return [(a, b) for a, b, _ in chosen]


class Speaker:
    def __init__(self):
        so = tts.sherpa()
        model = tts.ensure_model("speaker-recongition-models", SPEAKER_MODEL)   # (sic) release tag
        self.ex = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=4))

    def __call__(self, audio, sr):
        import numpy as np

        a16 = audio if sr == 16000 else np.interp(
            np.arange(0, len(audio) * 16000 // sr) * sr / 16000, np.arange(len(audio)), audio).astype(np.float32)
        s = self.ex.create_stream()
        s.accept_waveform(16000, a16)
        s.input_finished()
        v = np.array(self.ex.compute(s))
        return v / (np.linalg.norm(v) + 1e-9)


def main() -> None:
    p = argparse.ArgumentParser(description="Pick the best voice-cloning clip from a long recording")
    p.add_argument("recording")
    p.add_argument("--out-dir", default="voice-pick")
    p.add_argument("--max", type=int, default=14, help="Most candidate clips to test (default 14)")
    p.add_argument("--test-line", default=TEST_LINE)
    p.add_argument("--say", action="append", default=[], metavar="WORD=SPOKEN")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--clone-guidance", type=float, default=1.5)
    p.add_argument("--clone-steps", type=int, default=4)
    args = p.parse_args()

    import numpy as np
    import soundfile as sf

    say = dict(item.split("=", 1) for item in args.say if "=" in item)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("clip-*"):
        old.unlink()
    sr = tts.CLONE_SR
    audio = tts.decode(args.recording, sr)
    total = len(audio) / sr
    clips = candidates(audio, sr, args.max)
    if not clips:
        if total < MIN_S:
            sys.exit(f"Error: the recording is only {total:.1f}s; record at least 10 seconds.")
        clips = [(0, min(len(audio), int(MAX_S * sr)))]
    print(f"Recording: {total:.0f}s; testing {len(clips)} candidate clips.", flush=True)

    speaker = Speaker()
    # what "you" sound like: the average voice print over the whole recording, in 8 s windows
    win = 8 * sr
    prints = [speaker(audio[i:i + win], sr) for i in range(0, max(1, len(audio) - win // 2), win)]
    ref = np.mean(prints, axis=0)
    ref /= np.linalg.norm(ref) + 1e-9

    transcriber = tts.Transcriber()
    model = tts.load_zipvoice(args.clone_guidance)
    results = []
    for n, (a, b) in enumerate(clips, 1):
        print(f"PROGRESS {n}/{len(clips)}", flush=True)
        clip = tts.tidy(audio[a:b].copy(), sr, lead=0.0)
        text = transcriber(clip, sr)
        words = len(text.split())
        if words < 6:
            print(f"  clip {n}: skipped (too few words heard)", flush=True)
            continue
        db, _ = tts.frame_db(clip, sr)
        noise = float(np.percentile(db, 5) - np.percentile(db, 95))   # quieter is cleaner (more negative)
        gen = model.generate(tts.speakable(args.test_line, say), text, clip.tolist(), sr,
                             speed=args.speed, num_steps=args.clone_steps)
        test = tts.tidy(np.asarray(gen.samples, np.float32), gen.sample_rate)
        clarity = tts.accuracy(args.test_line, transcriber(test, gen.sample_rate))
        likeness = float(speaker(test, gen.sample_rate) @ ref)
        own = float(speaker(clip, sr) @ ref)
        score = likeness - max(0.0, 0.9 - clarity) * 0.5 - max(0.0, noise + 30) * 0.004
        stem = f"clip-{n:02d}"
        sf.write(out / f"{stem}.wav", clip, sr)
        tts.write_mp3([test], out / f"{stem}-test.mp3", gen.sample_rate)
        results.append({"id": stem, "start_s": round(a / sr, 2), "end_s": round(b / sr, 2),
                        "seconds": round(len(clip) / sr, 1), "transcript": text,
                        "likeness": round(likeness, 3), "clarity": round(clarity, 3),
                        "sample_likeness": round(own, 3), "noise_db": round(noise, 1), "score": round(score, 3),
                        "clip": f"{stem}.wav", "test": f"{stem}-test.mp3"})
        print(f"  clip {n} ({a / sr:.0f}s): likeness {likeness:.0%}, clarity {clarity:.0%} \"{text[:60]}\"", flush=True)
    if not results:
        sys.exit("Error: no usable clips found; record in a quiet room and leave short pauses between sentences.")
    results.sort(key=lambda r: -r["score"])
    (out / "results.json").write_text(json.dumps({"recording": str(args.recording), "test_line": args.test_line,
                                                  "results": results}, indent=2))
    best = results[0]
    print(f"Best: {best['id']} at {best['start_s']:.0f}s, likeness {best['likeness']:.0%} \"{best['transcript'][:70]}\"",
          flush=True)


if __name__ == "__main__":
    main()
