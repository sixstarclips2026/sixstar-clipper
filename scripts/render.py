#!/usr/bin/env python3
"""Six Star Clipper – rendert einen Stream-Ausschnitt zu einem 9:16-Clip.

Layouts: split (Facecam oben, Gameplay unten), full (Mitte-Crop), blur (16:9 auf unscharfem Hintergrund).
Overlays (ASS/libass): Hook oben, Credit, Werbekennzeichnung, Wort-Untertitel (optional, aus Whisper-JSON).
"""
import argparse, json, re, subprocess, sys, textwrap
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


def hook_markup_v2(text):
    """v2: schwarzer Text auf weißer Box, *Wort* in Rot."""
    out, on = "", False
    for part in text.split("*"):
        out += ("{\\c&H2A2AE8&}" if on else "{\\c&H101010&}") + esc(part)
        on = not on
    return out


def karaoke_lines(chunks):
    """v2: Wort-für-Wort-Untertitel – aktives Wort gelb und größer, jede Zeile springt kurz auf."""
    out = []
    for c in chunks:
        ws = c["words"]
        for i, w in enumerate(ws):
            st = w["start"] if i else c["start"]
            en = ws[i + 1]["start"] if i + 1 < len(ws) else c["end"]
            if en - st < 0.04:
                continue
            parts = []
            for k, x in enumerate(ws):
                word = esc(censor(x["word"].strip()).upper())
                if k == i:
                    parts.append("{\\c&H00E5FF&\\fscx112\\fscy112}" + word + "{\\c&HFFFFFF&\\fscx100\\fscy100}")
                else:
                    parts.append(word)
            pop = "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}" if i == 0 else ""
            out.append(f"Dialogue: 1,{ass_time(st)},{ass_time(en)},Sub,,0,0,0,,{pop}" + " ".join(parts))
    return out


def build_ass(args, dur, top_h, subs):
    hook_y = int(top_h if args.layout == "split" else H * 0.22)
    v2 = args.style == "v2"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Hook,{args.font},{args.hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,1,0,1,9,0,5,60,60,0,1",
        f"Style: Sub,{args.font},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,1,0,1,8,0,2,60,60,{int(H*0.22)},1",
        "Style: Credit,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,4,0,2,60,60,120,1",
        "Style: Disc,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,1,0,0,0,100,100,0,0,3,10,0,7,40,40,52,1",
        f"Style: HookBox,{args.font},{args.hook_size},&H00101010,&H00101010,&H00FFFFFF,&H00000000,0,0,0,0,100,100,1,0,3,18,0,5,60,60,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    end = ass_time(dur)
    if args.hook and v2:
        pop = "\\fscx70\\fscy70\\t(0,160,\\fscx106\\fscy106)\\t(160,260,\\fscx100\\fscy100)"
        lines.append(f"Dialogue: 2,0:00:00.00,{end},HookBox,,0,0,0,,{{\\an5\\pos({W//2},{hook_y}){pop}}}{hook_markup_v2(args.hook.upper())}")
    elif args.hook:
        lines.append(f"Dialogue: 2,0:00:00.00,{end},Hook,,0,0,0,,{{\\an5\\pos({W//2},{hook_y})}}{hook_markup(args.hook.upper())}")
    if args.credit:
        lines.append(f"Dialogue: 1,0:00:00.00,{end},Credit,,0,0,0,,{esc(args.credit)}")
    if args.disc:
        d_end = end if args.disc_sec <= 0 else ass_time(min(dur, args.disc_sec))
        lines.append(f"Dialogue: 3,0:00:00.00,{d_end},Disc,,0,0,0,,{esc(args.disc)}")
    if v2:
        lines += karaoke_lines(subs)
        subs = []
    for s in subs:
        lines.append(f"Dialogue: 1,{ass_time(s['start'])},{ass_time(s['end'])},Sub,,0,0,0,,{esc(censor(s['text']).upper())}")
    return "\n".join(lines) + "\n"


CENSOR = re.compile(r"\b(n+[i1]+g+(?:a|as|az|er|ers|uh)?|f+a+g+(?:s|got|gots)?|r+e+t+a+r+d+(?:s|ed)?)\b", re.I)


# Whisper verhört Streamer-Namen – Kampagnen verlangen korrekte Schreibweise
NAME_FIX = [(re.compile(p, re.I), r) for p, r in [
    (r"\bc[iy]pher\b", "Sypher"), (r"\bsiph?er\b", "Sypher"), (r"\bkai (?:sen|cen|cin|sin)n?att?\b", "Kai Cenat"),
    (r"\bski+ ?mask\b", "SkiiMask"), (r"\bmadd?(?:y|ie)\b", "Maddy"), (r"\breplay'?s\b", "Replays"),
]]


def censor(text):
    """Beleidigungen maskieren und bekannte Namen korrigieren (Plattform- und Kampagnenregeln)."""
    for rx, rep in NAME_FIX:
        text = rx.sub(rep, text)
    return CENSOR.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)


def word_chunks(words_json, max_words=3, max_gap=0.6, shift=0.0, until=1e9):
    words = json.loads(Path(words_json).read_text()) if words_json else []
    words = [dict(w, start=w["start"] + shift, end=min(w["end"], until) + shift) for w in words if w["start"] < until - 0.1]
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["start"] - cur[-1]["end"] > max_gap):
            chunks.append(cur); cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return [{"start": c[0]["start"], "end": c[-1]["end"] + 0.05, "words": c,
             "text": " ".join(x["word"].strip() for x in c)} for c in chunks]


def reaction_peaks(path, dur, max_n=3, gap=4.0, with_level=False):
    """Lauteste Reaktionsmomente (Momentary Loudness, 0,1-s-Raster) für Punch-in-Zooms, Cold Open und Pointen-Ende."""
    r = subprocess.run(["ffmpeg", "-nostats", "-v", "info", "-i", path, "-vn", "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    pts = [(float(t), float(m)) for t, m in re.findall(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(-?[\d.]+)", r)]
    if len(pts) < 20:
        return []
    ms = sorted(m for _, m in pts)
    med = ms[len(ms) // 2]
    cand = sorted((p for p in pts if p[1] > med + 6 and 1.0 < p[0] < dur - 1.5), key=lambda p: -p[1])
    out = []
    for t, m in cand:
        if all(abs(t - o[0]) >= gap for o in out):
            out.append((t, m))
        if len(out) >= max_n:
            break
    out.sort()
    return out if with_level else [t for t, _ in out]


def has_audio(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return bool(out.strip())


def story_cut(a, dur):
    """Dramaturgie vor dem Layout:
    - ending=punch: Ende = letzte große Reaktion + 1 Takt (Peak-End-Regel, Pause nach der Pointe)
    - teaser: Cold Open – die lauteste Reaktion 1,5 s vorab, weißer Blitz, dann der Clip von vorn (Neugierlücke)
    Gibt (neue Eingabedatei, Dauer, Zeitversatz der Untertitel, Ende im Original) zurück."""
    if not (a.teaser or a.ending == "punch") or not has_audio(a.input):
        return a.input, dur, 0.0, dur
    pk = reaction_peaks(a.input, dur, max_n=3, with_level=True)
    if not pk:
        print(json.dumps({"story": "keine Reaktionsspitze gefunden – unverändert"}))
        return a.input, dur, 0.0, dur
    end_t = dur
    if a.ending == "punch":
        e = min(dur, pk[-1][0] + a.beat)
        if 7 <= e < dur - 0.5:
            end_t = e
    shift, filt, n = 0.0, "", 1
    if a.teaser:
        p = max(pk, key=lambda x: x[1])[0]
        ts, te = max(0.0, p - 0.8), min(end_t, p + 0.7)
        shift = te - ts
        filt = (f"[0:v]trim={ts:.2f}:{te:.2f},setpts=PTS-STARTPTS[tv];[0:a]atrim={ts:.2f}:{te:.2f},asetpts=PTS-STARTPTS[ta];")
        n = 2
    filt += (f"[0:v]trim=0:{end_t:.2f},setpts=PTS-STARTPTS" + (",fade=t=in:st=0:d=0.3:color=white" if a.teaser else "") +
             f"[mv];[0:a]atrim=0:{end_t:.2f},asetpts=PTS-STARTPTS[ma];")
    filt += ("[tv][ta][mv][ma]" if a.teaser else "[mv][ma]") + f"concat=n={n}:v=1:a=1[v][a]"
    pre = str(Path(a.out).with_suffix(".pre.mp4"))
    subprocess.check_call(["ffmpeg", "-y", "-v", "error", "-i", a.input, "-filter_complex", filt, "-map", "[v]", "-map", "[a]",
                           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", pre])
    print(json.dumps({"story": {"ende": round(end_t, 2), "cold_open": round(shift, 2), "spitzen": [round(x[0], 1) for x in pk]}}))
    return pre, probe(pre)[2], shift, end_t


def zoom_expr(peaks, z=0.12, hold=1.1):
    """zoompan-Ausdruck: schnell rein (0,12 s), halten, weich raus (0,35 s)."""
    if not peaks:
        return "1"
    terms = []
    for p in peaks:
        a, b, c = p - 0.12, p + hold, p + hold + 0.35
        terms.append(f"if(between(it,{a:.2f},{p:.2f}),{z}*(it-{a:.2f})/0.12,"
                     f"if(between(it,{p:.2f},{b:.2f}),{z},if(between(it,{b:.2f},{c:.2f}),{z}*({c:.2f}-it)/0.35,0)))")
    return "1+" + "+".join(terms)


def measure_luma(path, dur, samples=8):
    """Mittlere Helligkeit (Y, 0–255) über einige Bilder der Quelle."""
    vals = []
    for k in range(samples):
        t = dur * (k + 0.5) / samples
        r = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", "scale=320:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
        m = re.findall(r"YAVG=([\d.]+)", r)
        if m:
            vals.append(float(m[-1]))
    return sum(vals) / len(vals) if vals else None


def light_filter(a, luma):
    """Dunkle Szenen aufhellen (Handy-Bildschirme sind klein, Nachtszenen saufen ab)."""
    if a.brightness == "off" or luma is None:
        return "", None
    if a.brightness != "auto":
        g = float(a.brightness)
    elif luma >= 70:
        return "", None
    else:
        g = min(1.8, (85 / max(luma, 15)) ** 0.6)
    return f"eq=gamma={g:.2f}:saturation=1.12,", round(g, 2)


def bleep_spans(words_json, shift=0.0, until=1e9):
    """Zeitfenster der Beleidigungen im Ton (aus den Whisper-Wörtern)."""
    if not words_json:
        return []
    out = []
    for w in json.loads(Path(words_json).read_text()):
        if w["start"] >= until - 0.1:
            continue
        if CENSOR.search(w["word"]):
            out.append((max(0.0, w["start"] + shift - 0.05), min(w["end"], until) + shift + 0.05))
    return out


# Facecam-Aufbereitung: Kompressionsblöcke glätten, sauber hochskalieren, leicht nachschärfen
FC_CLEAN = "hqdn3d=1.5:1.5:3:3"
FC_SHARP = "unsharp=5:5:0.8:3:3:0.0"


def face_panel(fw, fh, fx, fy, top_h, maxup):
    """Facecam-Feld oben. Vergrößerung höchstens maxup (1.0 = Originalgröße, gestochen scharf).
    Ist die Facecam schmaler als das Feld, füllt eine unscharfe, abgedunkelte Kopie den Rest."""
    s = min(maxup, W / fw)
    crop = f"crop={fw}:{fh}:{fx}:{fy},{FC_CLEAN}"
    if s * fw >= W - 2:
        return round(W / fw, 2), f"{crop},scale={W}:{top_h}:flags=lanczos" + (f",{FC_SHARP}" if s > 1.05 else "")
    pw, ph = even(fw * s), even(fh * s)
    fg = f"scale={pw}:{ph}:flags=lanczos" + (f",{FC_SHARP}" if s > 1.05 else "")
    return round(s, 2), (f"{crop},split=2[fa][fb];[fa]scale={W}:{top_h},boxblur=25:2,eq=brightness=-0.3[fbg];"
                         f"[fb]{fg}[ffg];[fbg][ffg]overlay=(W-w)/2:(H-h)/2")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--layout", default="auto", choices=["auto", "split", "full", "blur"])
    ap.add_argument("--fc-corner", default="tr", choices=["tl", "tr", "bl", "br"])
    ap.add_argument("--fc-size", type=float, default=0.26, help="Facecam-Breite als Anteil der Quellbreite")
    ap.add_argument("--split", type=float, default=0.32, help="Anteil oben (Facecam)")
    ap.add_argument("--fc-maxup", type=float, default=1.0, help="Facecam höchstens so stark vergrößern; 1.0 = Originalgröße, Rest unscharfer Hintergrund")
    ap.add_argument("--zoom", type=float, default=1.0)
    ap.add_argument("--gpx", type=float, default=0.5, help="Gameplay links/rechts 0..1")
    ap.add_argument("--hook", default="")
    ap.add_argument("--hook-size", type=int, default=84)
    ap.add_argument("--credit", default="")
    ap.add_argument("--disc", default="#ad")
    ap.add_argument("--disc-sec", type=float, default=3)
    ap.add_argument("--words", default="", help="Whisper-Wort-JSON für Untertitel")
    ap.add_argument("--font", default="Anton")
    ap.add_argument("--brightness", default="auto", help="auto | off | Gamma-Wert (z. B. 1.4)")
    ap.add_argument("--bleep", default="on", choices=["on", "off"], help="Piepton über Beleidigungen im Ton")
    ap.add_argument("--fc-box", default="", help="Facecam manuell: x,y,w,h als Anteile der Quelle (0–1)")
    ap.add_argument("--teaser", action="store_true", help="Cold Open: lauteste Reaktion 1,5 s vorab zeigen")
    ap.add_argument("--ending", default="natural", choices=["natural", "punch"], help="punch: Ende auf letzter Reaktion + 1 Takt")
    ap.add_argument("--beat", type=float, default=1.3, help="Takt nach der Pointe in Sekunden")
    ap.add_argument("--style", default="v2", choices=["v1", "v2"], help="v2: Karaoke-Untertitel, Hook-Box, Punch-in-Zooms, Fortschrittsbalken")
    a = ap.parse_args()

    vw, vh, dur = probe(a.input)
    a.input, dur, sub_shift, sub_until = story_cut(a, dur)
    top_h = even(H * a.split)
    face = None
    fc_up = None
    if a.layout == "auto":
        face = detect_facecam(a.input, vw, vh, dur)
        a.layout = "split" if face else "blur"
        print(json.dumps({"auto_layout": a.layout, "face": face}))
    if a.layout == "split" and face:
        cx, cy, fwid = face
        fw = even(min(vw, max(fwid * 3.4, vw * 0.21))); fh = even(fw / (W / top_h))
        if fh > vh:
            fh = even(vh); fw = even(fh * (W / top_h))
        fx = even(min(max(0, cx - fw / 2), vw - fw)); fy = even(min(max(0, cy - fh * 0.45), vh - fh))
        gw, gh, gx, gy = crop_for(vw, vh, W / (H - top_h), a.zoom, a.gpx, 0.5)
        fc_up, panel = face_panel(fw, fh, fx, fy, top_h, a.fc_maxup)
        vf = (f"[0:v]split=2[a][b];[a]{panel}[t];"
              f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{H-top_h}:flags=lanczos[g];[t][g]vstack,"
              f"drawbox=x=0:y={top_h-3}:w={W}:h=6:color=0xff3e9d:t=fill")
    elif a.layout == "split":
        fw, fh, _, _ = crop_for(vw, vh, W / top_h, 1.0, 0, 0)
        fw = even(min(vw * a.fc_size, fw)); fh = even(fw / (W / top_h))
        fx = even((vw - fw) * (1 if a.fc_corner[1] == "r" else 0))
        fy = even((vh - fh) * (1 if a.fc_corner[0] == "b" else 0))
        if a.fc_box:
            bx, by, bw, bh = [float(v) for v in a.fc_box.split(",")]
            cx, cy = (bx + bw / 2) * vw, (by + bh / 2) * vh
            fw = even(min(vw, max(bw * vw, bh * vh * W / top_h))); fh = even(fw / (W / top_h))
            if fh > vh:
                fh = even(vh); fw = even(fh * W / top_h)
            fx = even(min(max(0, cx - fw / 2), vw - fw)); fy = even(min(max(0, cy - fh / 2), vh - fh))
        gw, gh, gx, gy = crop_for(vw, vh, W / (H - top_h), a.zoom, a.gpx, 0.5)
        fc_up, panel = face_panel(fw, fh, fx, fy, top_h, a.fc_maxup)
        vf = (f"[0:v]split=2[a][b];[a]{panel}[t];"
              f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{H-top_h}:flags=lanczos[g];[t][g]vstack,"
              f"drawbox=x=0:y={top_h-3}:w={W}:h=6:color=0xff3e9d:t=fill")
    elif a.layout == "full":
        cw, ch, cx, cy = crop_for(vw, vh, 9 / 16, a.zoom, a.gpx, 0.5)
        vf = f"[0:v]crop={cw}:{ch}:{cx}:{cy},scale={W}:{H}"
    else:
        sw, sh = even(vw / a.zoom), even(vh / a.zoom)
        fg_h = even(W * sh / sw)
        vf = (f"[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=-0.25[bg];"
              f"[b]crop={sw}:{sh}:{even((vw-sw)*a.gpx)}:{even((vh-sh)/2)},scale={W}:{fg_h}[fg];[bg][fg]overlay=0:(H-h)/2")

    # 60-fps-Quellen (Twitch) zuerst auf 30 fps: zoompan erzeugt sonst Zeitlupe mit doppelter Länge
    luma = measure_luma(a.input, dur)
    lf, gamma = light_filter(a, luma)
    vf = vf.replace("[0:v]", "[0:v]fps=30," + lf, 1)
    meta = {"layout": a.layout, "face": face, "luma": round(luma, 1) if luma else None, "gamma": gamma, "fc_upscale": fc_up}
    ass = Path(a.out).with_suffix(".ass")
    ass.write_text(build_ass(a, dur, top_h, word_chunks(a.words, shift=sub_shift, until=sub_until)), encoding="utf-8")
    if a.style == "v2":
        peaks = reaction_peaks(a.input, dur)
        meta["punch_ins"] = peaks
        print(json.dumps({"punch_ins": peaks}))
        if peaks:
            vf += (f",zoompan=z='{zoom_expr(peaks)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                   f":d=1:s={W}x{H}:fps=30")
        vf += f",ass={ass.as_posix()}[base];color=c=0xFFE500:s={W}x14:r=30[bar];"
        vf += f"[base][bar]overlay=x='-{W}+{W}*t/{dur:.2f}':y={H-14}:shortest=1[v]"
    else:
        vf += f",ass={ass.as_posix()}[v]"
    spans = bleep_spans(a.words, sub_shift, sub_until) if a.bleep == "on" and has_audio(a.input) else []
    meta["bleeps"] = [[round(s, 2), round(e, 2)] for s, e in spans]
    if spans:
        on = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e in spans)
        vf += (f";[0:a]volume=0:enable='{on}'[mute];sine=f=1000:sample_rate=48000,volume=0.3,volume=0:enable='not({on})'[tone];"
               f"[mute][tone]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
        amap, af = ["-map", "[aout]"], []
    else:
        amap, af = ["-map", "0:a?"], ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd = (["ffmpeg", "-y", "-v", "error", "-i", a.input, "-filter_complex", vf, "-map", "[v]"] + amap +
           ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30"] + af +
           ["-ar", "48000", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", a.out])
    subprocess.check_call(cmd)
    Path(a.out).with_suffix(".meta.json").write_text(json.dumps(meta))
    print(json.dumps({"render": meta}))
    print(json.dumps({"out": a.out, "duration": round(dur, 2), "layout": a.layout}))


if __name__ == "__main__":
    sys.exit(main())
