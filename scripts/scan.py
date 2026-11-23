#!/usr/bin/env python3
"""Moment-Detektor: findet Höhepunkte in langen Stream-Aufnahmen.

1. Lautheit pro Sekunde (ffmpeg ebur128) -> Ausschläge gegenüber dem gleitenden Normalpegel
   (Schreien, Lachen, Ausrasten).
2. Die stärksten Ausschläge werden mit Whisper transkribiert und nach Reaktions-Wörtern bewertet.
3. Ausgabe: Kandidaten-JSON mit Zeitfenster, Punktzahl und Transkript, damit Claude die besten
   Momente auswählt und ehrliche Hooks schreibt.
"""
import json, re, statistics, subprocess, sys
from pathlib import Path

REACT = re.compile(r"\b(no way|oh my god|omg|what the|wtf|bro+|bruh|yo+|nah+|let'?s go+|wait|chat|crazy|insane|"
                   r"holy|dude|why|stop|run|help|hahah\w*|lmao|lol|screaming|oh no|oh shit|bro what|are you serious|"
                   r"look at this|did you see|clip (that|it))\b", re.I)


def loudness(src):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", src, "-vn", "-af", "ebur128=peak=none",
                        "-f", "null", "-"], capture_output=True, text=True)
    per_sec = {}
    for line in p.stderr.splitlines():
        m = re.search(r"t:\s*([\d.]+).*?M:\s*(-?[\d.]+)", line)
        if m:
            t, val = float(m.group(1)), float(m.group(2))
            if val > -70:
                per_sec.setdefault(int(t), []).append(val)
    n = max(per_sec) + 1 if per_sec else 0
    return [max(per_sec.get(i, [-70])) for i in range(n)]


def peaks(lv, top_k=14, win=300, min_gap=90):
    n = len(lv)
    smooth = [sum(lv[max(0, i - 1):i + 2]) / len(lv[max(0, i - 1):i + 2]) for i in range(n)]
    scores = []
    for i in range(0, n, 1):
        lo, hi = max(0, i - win // 2), min(n, i + win // 2)
        base = statistics.median(lv[lo:hi:5] or [-70])
        scores.append(smooth[i] - base)
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)
    chosen = []
    for i in order:
        if all(abs(i - c) >= min_gap for c in chosen):
            chosen.append(i)
        if len(chosen) >= top_k:
            break
    return [(c, round(scores[c], 2)) for c in sorted(chosen)]


def transcribe_windows(src, cands, pre=25, post=15):
    from faster_whisper import WhisperModel
    model = WhisperModel("base", device="cpu", compute_type="int8")
    out = []
    for t, loud in cands:
        s = max(0, t - pre)
        wav = f"/tmp/w_{t}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(s), "-t", str(pre + post), "-i", src,
                        "-ac", "1", "-ar", "16000", wav], check=True)
        segs, _ = model.transcribe(wav, language="en", vad_filter=True)
        segs = list(segs)
        text = " ".join(x.text.strip() for x in segs)
        hits = len(REACT.findall(text))
        words = len(text.split())
        score = round(loud * 1.0 + hits * 1.5 + min(words, 80) / 40, 2)
        # Clip so schneiden, dass er auf einem Satzanfang startet und kurz nach dem Höhepunkt endet
        starts = [s + x.start for x in segs if s + x.start <= t - 3]
        clip_start = max(s, (starts[-3] if len(starts) >= 3 else (starts[0] if starts else t - 20)))
        clip_end = min(t + 12, clip_start + 55)
        out.append({"peak": t, "start": round(clip_start, 1), "end": round(clip_end, 1),
                    "loud": loud, "reactions": hits, "score": score, "text": text[:700]})
    return sorted(out, key=lambda x: x["score"], reverse=True)


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


if __name__ == "__main__":
    src, out_path = sys.argv[1], sys.argv[2]
    offset = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0   # Start des gescannten Abschnitts im VOD
    lv = loudness(src)
    cands = transcribe_windows(src, peaks(lv))
    for c in cands:
        c["vod_start"], c["vod_end"] = hms(c["start"] + offset), hms(c["end"] + offset)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps({"seconds_scanned": len(lv), "candidates": cands}, indent=1))
    print(f"{len(lv)} s gescannt, {len(cands)} Kandidaten")
