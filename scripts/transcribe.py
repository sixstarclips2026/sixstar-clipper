#!/usr/bin/env python3
"""Wort-genaue englische Untertitel mit faster-whisper (läuft kostenlos auf dem GitHub-Runner)."""
import json, sys
from faster_whisper import WhisperModel

src, out = sys.argv[1], sys.argv[2]
lang = sys.argv[3] if len(sys.argv) > 3 else "en"
model = WhisperModel("base", device="cpu", compute_type="int8")
segments, _ = model.transcribe(src, language=lang, word_timestamps=True, vad_filter=True)
words = [{"word": w.word, "start": round(w.start, 2), "end": round(w.end, 2)}
         for s in segments for w in (s.words or [])]
json.dump(words, open(out, "w"))
print(f"{len(words)} Wörter")
