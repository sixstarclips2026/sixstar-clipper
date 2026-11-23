"""Qualitäts-Tor: prüft jeden fertigen Clip, bevor er veröffentlicht werden darf.

Aufruf: python scripts/qa.py CLIP.mp4 [words.json] [qa.json]
Exit-Code 1 = durchgefallen (Clip wird nicht hochgeladen).
"""
import json, re, subprocess, sys

FOREIGN = re.compile(r"media share|caption|tiktok|this video|youtube|watch this|reacting to|instagram reel", re.I)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,duration:format=duration",
                          "-of", "json", path], capture_output=True, text=True).stdout
    return json.loads(out)


def rms_db(path, start, dur):
    """Mittlere Lautheit (RMS dB) eines Abschnitts; -99 wenn stumm/kein Ton."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(dur), "-i", path, "-vn",
                        "-af", "astats=metadata=0:reset=0", "-f", "null", "-"], capture_output=True, text=True).stderr
    vals = [float(m) for m in re.findall(r"RMS level dB:\s*(-?[\d.]+)", r)]
    return vals[-1] if vals else -99.0


def silence_share(path, dur):
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-vn", "-af", "silencedetect=n=-45dB:d=1.5",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    total = sum(float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", r))
    return total / dur if dur else 1.0


def main():
    clip = sys.argv[1]
    words = sys.argv[2] if len(sys.argv) > 2 else ""
    out = sys.argv[3] if len(sys.argv) > 3 else ""
    fails, warns = [], []

    info = probe(clip)
    streams = info.get("streams", [])
    v = next((s for s in streams if s["codec_type"] == "video"), {})
    has_audio = any(s["codec_type"] == "audio" for s in streams)
    dur = float(info.get("format", {}).get("duration", 0))

    a = next((s for s in streams if s["codec_type"] == "audio"), {})
    if v.get("duration") and a.get("duration") and abs(float(v["duration"]) - float(a["duration"])) > 1.0:
        fails.append(f"Bild und Ton laufen auseinander (Bild {float(v['duration']):.1f} s, Ton {float(a['duration']):.1f} s)")
    if (v.get("width"), v.get("height")) != (1080, 1920):
        fails.append(f"Format {v.get('width')}x{v.get('height')} statt 1080x1920")
    if dur < 7:
        fails.append(f"zu kurz ({dur:.1f} s, Minimum 7 s)")
    if dur > 90:
        warns.append(f"lang ({dur:.0f} s) – Shorts/TikTok-Retention prüfen")
    if not has_audio:
        fails.append("keine Tonspur")
    else:
        head = rms_db(clip, 0, 2)
        if head < -50:
            fails.append(f"Ton fehlt am Anfang (erste 2 s: {head:.0f} dB)")
        share = silence_share(clip, dur)
        if share > 0.35:
            fails.append(f"zu viel Stille ({share:.0%} des Clips)")

    text = ""
    if words:
        try:
            text = " ".join(w["word"] for w in json.load(open(words)))
        except Exception:
            pass
    if text:
        hits = sorted(set(m.lower() for m in FOREIGN.findall(text)))
        if hits:
            fails.append("Verdacht Media-Share/fremdes Video: " + ", ".join(hits))
        if len(text.split()) < 12:
            warns.append("kaum Sprache – Hook muss allein tragen")

    res = {"clip": clip, "duration": round(dur, 1), "passed": not fails, "fails": fails, "warns": warns}
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if out:
        json.dump(res, open(out, "w"), ensure_ascii=False, indent=2)
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
