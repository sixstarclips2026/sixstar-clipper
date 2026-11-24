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


def center_luma(path, dur, samples=6):
    """Helligkeit der Bildmitte (ohne Hook-Box und Untertitel)."""
    vals = []
    for k in range(samples):
        r = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{dur * (k + 0.5) / samples:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", "crop=iw:ih*0.4:0:ih*0.3,scale=270:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                            "-f", "null", "-"], capture_output=True, text=True).stderr
        m = re.findall(r"YAVG=([\d.]+)", r)
        if m:
            vals.append(float(m[-1]))
    return sum(vals) / len(vals) if vals else None


def face_share(path, dur, samples=6):
    """Größtes erkanntes Gesicht als Anteil der Bildbreite (None = OpenCV fehlt)."""
    try:
        import cv2
    except ImportError:
        return None
    try:
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if casc.empty():
            return None
    except Exception:
        return None
    best = 0.0
    for k in range(samples):
        png = f"/tmp/qa_{k}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * (k + 0.5) / samples:.2f}", "-i", path,
                        "-frames:v", "1", png])
        img = cv2.imread(png)
        if img is None:
            continue
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for (x, y, w, h) in casc.detectMultiScale(g, 1.1, 5, minSize=(30, 30)):
            best = max(best, w / img.shape[1])
    return round(best, 3)


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
    if dur > 60:
        fails.append(f"zu lang ({dur:.0f} s) – Handbuch: Ziel 15–30 s, max. 45 s; mit \"ending\": \"punch\" kürzen")
    elif dur > 45:
        warns.append(f"lang ({dur:.0f} s) – über 45 s fällt die Wiedergabequote meist deutlich ab")
    luma = center_luma(clip, dur) if dur else None
    if luma is not None:
        if luma < 35:
            fails.append(f"zu dunkel (Bildmitte {luma:.0f}/255) – auf dem Handy nicht erkennbar")
        elif luma < 55:
            warns.append(f"eher dunkel (Bildmitte {luma:.0f}/255)")
    fs = face_share(clip, dur) if dur else None
    if fs is not None:
        if fs == 0:
            warns.append("kein Gesicht erkannt – Kampagnen mit Facecam-Pflicht prüfen (Layout split / fc_box)")
        elif fs < 0.08:
            warns.append(f"Streamer-Gesicht sehr klein ({fs:.0%} der Breite) – Reaktion kaum sichtbar")
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

    meta = {}
    try:
        meta = json.load(open(re.sub(r"\.mp4$", ".meta.json", clip)))
    except Exception:
        pass
    up = meta.get("fc_upscale") if isinstance(meta, dict) else None
    if up and up > 2.6:
        warns.append(f"Facecam {up}x vergrößert – wirkt verpixelt (Ziel höchstens 2,2x)")
    res = {"clip": clip, "duration": round(dur, 1), "passed": not fails, "fails": fails, "warns": warns,
           "luma": round(luma, 1) if luma is not None else None, "face_share": fs, "render": meta}
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if out:
        json.dump(res, open(out, "w"), ensure_ascii=False, indent=2)
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
