#!/usr/bin/env python3
"""Six Star Clipper – rendert einen Stream-Ausschnitt zu einem 9:16-Clip.

Layouts: split (Facecam oben, Gameplay unten), full (Mitte-Crop), blur (16:9 auf unscharfem Hintergrund).
Overlays (ASS/libass): Hook oben, Credit, Werbekennzeichnung, Wort-Untertitel (optional, aus Whisper-JSON).
"""
import argparse, json, subprocess, sys, textwrap
from pathlib import Path

W, H = 1080, 1920


def probe(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)])
    j = json.loads(out)
    s = j["streams"][0]
    return int(s["width"]), int(s["height"]), float(j["format"]["duration"])


def even(x):
    return max(2, int(x) // 2 * 2)


def crop_for(vw, vh, aspect, zoom, px, py):
    h = vh / zoom
    w = h * aspect
    if w > vw:
        w = vw
        h = w / aspect
    return even(w), even(h), even((vw - w) * px), even((vh - h) * py)


def detect_facecam(path, vw, vh, dur, samples=8):
    """Sucht eine fest stehende Facecam (Gesicht an immer derselben Stelle). Rückgabe: (x, y, w, h) oder None."""
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
    hits = []
    for k in range(samples):
        t = dur * (k + 0.5) / samples
        png = f"/tmp/fc_{k}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", png])
        img = cv2.imread(png)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = casc.detectMultiScale(gray, 1.08, 5, minSize=(int(vw * 0.015), int(vw * 0.015)))
        for (x, y, w, h) in faces:
            if w < vw * 0.25:  # Facecam-Gesichter sind klein im Bild
                hits.append((x + w / 2, y + h / 2, w))
    if len(hits) < max(3, samples // 3):
        return None
    # größter Cluster von Gesichtsmitten, die nahe beieinander liegen
    best = []
    for c in hits:
        group = [h for h in hits if abs(h[0] - c[0]) < vw * 0.06 and abs(h[1] - c[1]) < vh * 0.06]
        if len(group) > len(best):
            best = group
    if len(best) < max(3, samples // 3):
        return None
    cx = sum(h[0] for h in best) / len(best)
    cy = sum(h[1] for h in best) / len(best)
    fw = sum(h[2] for h in best) / len(best)
    return cx, cy, fw


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def esc(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def hook_markup(text):
    """*Wort* -> gelb hervorgehoben."""
    out, on = "", False
    for part in text.split("*"):
        out += ("{\\c&H4DE1FF&}" if on else "{\\c&HFFFFFF&}") + esc(part)
        on = not on
    return out


def build_ass(args, dur, top_h, subs):
    hook_y = int(top_h if args.layout == "split" else H * 0.22)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Hook,{args.font},{args.hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,1,0,1,9,0,5,60,60,0,1",
        f"Style: Sub,{args.font},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,1,0,1,8,0,2,60,60,{int(H*0.22)},1",
        "Style: Credit,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,4,0,2,60,60,120,1",
        "Style: Disc,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,1,0,0,0,100,100,0,0,3,10,0,7,40,40,52,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    end = ass_time(dur)
    if args.hook:
        lines.append(f"Dialogue: 2,0:00:00.00,{end},Hook,,0,0,0,,{{\\an5\\pos({W//2},{hook_y})}}{hook_markup(args.hook.upper())}")
    if args.credit:
        lines.append(f"Dialogue: 1,0:00:00.00,{end},Credit,,0,0,0,,{esc(args.credit)}")
    if args.disc:
        d_end = end if args.disc_sec <= 0 else ass_time(min(dur, args.disc_sec))
        lines.append(f"Dialogue: 3,0:00:00.00,{d_end},Disc,,0,0,0,,{esc(args.disc)}")
    for s in subs:
        lines.append(f"Dialogue: 1,{ass_time(s['start'])},{ass_time(s['end'])},Sub,,0,0,0,,{esc(s['text'].upper())}")
    return "\n".join(lines) + "\n"


def word_chunks(words_json, max_words=3, max_gap=0.6):
    words = json.loads(Path(words_json).read_text()) if words_json else []
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["start"] - cur[-1]["end"] > max_gap):
            chunks.append(cur); cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return [{"start": c[0]["start"], "end": c[-1]["end"] + 0.05, "text": " ".join(x["word"].strip() for x in c)} for c in chunks]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--layout", default="auto", choices=["auto", "split", "full", "blur"])
    ap.add_argument("--fc-corner", default="tr", choices=["tl", "tr", "bl", "br"])
    ap.add_argument("--fc-size", type=float, default=0.26, help="Facecam-Breite als Anteil der Quellbreite")
    ap.add_argument("--split", type=float, default=0.40, help="Anteil oben (Facecam)")
    ap.add_argument("--zoom", type=float, default=1.0)
    ap.add_argument("--gpx", type=float, default=0.5, help="Gameplay links/rechts 0..1")
    ap.add_argument("--hook", default="")
    ap.add_argument("--hook-size", type=int, default=84)
    ap.add_argument("--credit", default="")
    ap.add_argument("--disc", default="#ad")
    ap.add_argument("--disc-sec", type=float, default=3)
    ap.add_argument("--words", default="", help="Whisper-Wort-JSON für Untertitel")
    ap.add_argument("--font", default="Anton")
    a = ap.parse_args()

    vw, vh, dur = probe(a.input)
    top_h = even(H * a.split)
    face = None
    if a.layout == "auto":
        face = detect_facecam(a.input, vw, vh, dur)
        a.layout = "split" if face else "blur"
        print(json.dumps({"auto_layout": a.layout, "face": face}))
    if a.layout == "split" and face:
        cx, cy, fwid = face
        fw = even(min(vw, fwid * 3.4)); fh = even(fw / (W / top_h))
        if fh > vh:
            fh = even(vh); fw = even(fh * (W / top_h))
        fx = even(min(max(0, cx - fw / 2), vw - fw)); fy = even(min(max(0, cy - fh * 0.45), vh - fh))
        gw, gh, gx, gy = crop_for(vw, vh, W / (H - top_h), a.zoom, a.gpx, 0.5)
        vf = (f"[0:v]split=2[a][b];[a]crop={fw}:{fh}:{fx}:{fy},scale={W}:{top_h}[t];"
              f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{H-top_h}[g];[t][g]vstack,"
              f"drawbox=x=0:y={top_h-3}:w={W}:h=6:color=0xff3e9d:t=fill")
    elif a.layout == "split":
        fw, fh, _, _ = crop_for(vw, vh, W / top_h, 1.0, 0, 0)
        fw = even(min(vw * a.fc_size, fw)); fh = even(fw / (W / top_h))
        fx = even((vw - fw) * (1 if a.fc_corner[1] == "r" else 0))
        fy = even((vh - fh) * (1 if a.fc_corner[0] == "b" else 0))
        gw, gh, gx, gy = crop_for(vw, vh, W / (H - top_h), a.zoom, a.gpx, 0.5)
        vf = (f"[0:v]split=2[a][b];[a]crop={fw}:{fh}:{fx}:{fy},scale={W}:{top_h}[t];"
              f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{H-top_h}[g];[t][g]vstack,"
              f"drawbox=x=0:y={top_h-3}:w={W}:h=6:color=0xff3e9d:t=fill")
    elif a.layout == "full":
        cw, ch, cx, cy = crop_for(vw, vh, 9 / 16, a.zoom, a.gpx, 0.5)
        vf = f"[0:v]crop={cw}:{ch}:{cx}:{cy},scale={W}:{H}"
    else:
        sw, sh = even(vw / a.zoom), even(vh / a.zoom)
        fg_h = even(W * sh / sw)
        vf = (f"[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=-0.25[bg];"
              f"[b]crop={sw}:{sh}:{even((vw-sw)*a.gpx)}:{even((vh-sh)/2)},scale={W}:{fg_h}[fg];[bg][fg]overlay=0:(H-h)/2")

    ass = Path(a.out).with_suffix(".ass")
    ass.write_text(build_ass(a, dur, top_h, word_chunks(a.words)), encoding="utf-8")
    vf += f",ass={ass.as_posix()}[v]"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", a.input, "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", a.out]
    subprocess.check_call(cmd)
    print(json.dumps({"out": a.out, "duration": round(dur, 2), "layout": a.layout}))


if __name__ == "__main__":
    sys.exit(main())
