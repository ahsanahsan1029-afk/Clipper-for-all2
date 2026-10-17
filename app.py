"""
Clip Maker backend
YouTube link -> download (yt-dlp) -> transcript (faster-whisper)
-> best moments choose -> cut + burned captions (ffmpeg)
"""
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path

import yt_dlp
from flask import Flask, jsonify, request, send_from_directory

BASE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", BASE / "data"))
DATA.mkdir(parents=True, exist_ok=True)

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")  # tiny/base/small/medium
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "60"))
COOKIES_FILE = os.environ.get("COOKIES_FILE")  # optional, YouTube block se bachne ke liye
KEEP_SECONDS = 6 * 3600

app = Flask(__name__)
jobs = {}
gate = threading.Semaphore(1)  # ek time par ek hi heavy job
_model = None
_model_lock = threading.Lock()

YT_RE = re.compile(r"^https?://([a-z0-9-]+\.)?(youtube\.com|youtu\.be)/", re.I)
SENT_END = re.compile(r"[.?!।]$")


# ---------- helpers ----------
def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        return _model


def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg error: " + (p.stderr or "")[-400:])


def purge_old():
    now = time.time()
    for d in DATA.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > KEEP_SECONDS:
            shutil.rmtree(d, ignore_errors=True)
            jobs.pop(d.name, None)


def update(job, stage=None, progress=None):
    if stage is not None:
        job["stage"] = stage
    if progress is not None:
        job["progress"] = int(progress)


# ---------- pipeline steps ----------
def download(url, folder, job):
    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                update(job, progress=5 + 20 * d.get("downloaded_bytes", 0) / total)

    opts = {
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "outtmpl": str(folder / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if (info.get("duration") or 0) > MAX_MINUTES * 60:
            raise ValueError(f"Video {MAX_MINUTES} minute se lamba hai. Chhota video try karo.")
        job["title"] = info.get("title", "")
        ydl.download([url])

    files = sorted(folder.glob("source.*"))
    if not files:
        raise RuntimeError("Video file nahi mili.")
    return files[0]


def transcribe(src, folder, job):
    audio = folder / "audio.wav"
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(audio)])
    segments, info = get_model().transcribe(
        str(audio), word_timestamps=True, vad_filter=True
    )
    total = info.duration or 1
    job["duration"] = total
    segs, words = [], []
    for s in segments:
        text = s.text.strip()
        if text:
            segs.append({"start": s.start, "end": s.end, "text": text})
        for w in s.words or []:
            words.append({"start": w.start, "end": w.end, "word": w.word.strip()})
        update(job, progress=30 + 40 * min(s.end / total, 1))
    return segs, words


def pick_clips(segs, n, lo, hi):
    """Heuristic: dense speech, questions/exclamations, clean sentence end."""
    cands = []
    for i in range(len(segs)):
        for j in range(i, len(segs)):
            dur = segs[j]["end"] - segs[i]["start"]
            if dur > hi:
                break
            if dur < lo:
                continue
            text = " ".join(x["text"] for x in segs[i : j + 1])
            score = len(text.split()) / dur
            score += 0.3 * (text.count("?") + text.count("!"))
            if SENT_END.search(segs[j]["text"]):
                score += 0.4
            cands.append((score, i, j))
    cands.sort(reverse=True)

    chosen = []
    for score, i, j in cands:
        a, b = segs[i]["start"], segs[j]["end"]
        if all(b + 1 < c["start"] or a - 1 > c["end"] for c in chosen):
            chosen.append({"start": a, "end": b, "i": i, "j": j})
        if len(chosen) >= n:
            break
    chosen.sort(key=lambda c: c["start"])
    for c in chosen:
        text = " ".join(x["text"] for x in segs[c["i"] : c["j"] + 1])
        first = re.split(r"(?<=[.?!।])\s", text)[0]
        c["title"] = (first[:60] + "…") if len(first) > 60 else first
        body = (text[:200] + "…") if len(text) > 200 else text
        c["caption"] = f"{body} #shorts #viral"
    return chosen


def ass_time(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def make_ass(words, start, end, path, w, h, vertical):
    ws = [x for x in words if x["start"] >= start - 0.05 and x["end"] <= end + 0.05]
    per = 3 if vertical else 5
    size = int(min(w, h) * (0.085 if vertical else 0.075))
    margin_v = int(h * (0.2 if vertical else 0.08))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,Noto Sans,{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,{max(3, size // 12)},1,2,40,40,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    k = 0
    while k < len(ws):
        chunk = ws[k : k + per]
        k += per
        a = max(chunk[0]["start"] - start, 0)
        b = max(chunk[-1]["end"] - start, a + 0.2)
        text = " ".join(x["word"] for x in chunk).replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,,0,0,0,,{text}")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def cut_clip(src, words, c, out_dir, idx, vertical, captions):
    out = out_dir / f"clip_{idx}.mp4"
    w, h = (720, 1280) if vertical else (1280, 720)
    chain = []
    if vertical:
        chain.append("crop='min(iw,ih*9/16)':ih")
        chain.append("scale=720:1280:force_original_aspect_ratio=increase")
        chain.append("crop=720:1280")
    else:
        chain.append("scale=-2:720")
    if captions:
        make_ass(words, c["start"], c["end"], out_dir / f"clip_{idx}.ass", w, h, vertical)
        chain.append(f"subtitles=clip_{idx}.ass")
    run(
        [
            "ffmpeg", "-y",
            "-ss", f"{c['start']:.2f}", "-i", str(src),
            "-t", f"{c['end'] - c['start']:.2f}",
            "-vf", ",".join(chain),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            out.name,
        ],
        cwd=out_dir,  # relative path => subtitles filter me escaping ki dikkat nahi
    )
    return out


def friendly(e):
    if isinstance(e, yt_dlp.utils.DownloadError):
        detail = re.sub(r"\x1b\[[0-9;]*m", "", str(e))[-220:]
        return ("Video download nahi ho paaya. Link check karo. Private ya age-restricted "
                "video ho sakta hai, ya YouTube ne server ko block kiya hai (README me cookies dekho). "
                f"Asli error: {detail}")
    return str(e) or "Kuch gadbad ho gayi."


def process(jid, url, n, lo, hi, vertical, captions):
    job = jobs[jid]
    folder = DATA / jid
    clips_dir = folder / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    try:
        update(job, "Line me hai...", 1)
        with gate:
            update(job, "Video download ho raha hai...", 5)
            src = download(url, folder, job)

            update(job, "Awaaz sun ke text bana rahe hain...", 28)
            segs, words = transcribe(src, folder, job)
            if not segs:
                raise ValueError("Is video me bolne wali awaaz nahi mili.")

            update(job, "Best moments dhoondh rahe hain...", 72)
            plan = pick_clips(segs, n, lo, hi)
            if not plan:
                raise ValueError("Itne lambe clips banane layak hissa nahi mila. Length kam karke try karo.")
            job["plan"] = [{"n": i + 1, "start": c["start"], "end": c["end"]} for i, c in enumerate(plan)]

            for i, c in enumerate(plan, start=1):
                update(job, f"Clip {i}/{len(plan)} ban raha hai...", 74 + 25 * (i - 1) / len(plan))
                cut_clip(src, words, c, clips_dir, i, vertical, captions)
                job["clips"].append({
                    "n": i,
                    "start": c["start"],
                    "end": c["end"],
                    "title": c["title"],
                    "caption": c["caption"],
                    "video": f"/clips/{jid}/clip_{i}.mp4",
                })
        update(job, "Ho gaya", 100)
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        job.update(status="error", error=friendly(e))
    finally:
        for f in folder.glob("source.*"):
            f.unlink(missing_ok=True)
        (folder / "audio.wav").unlink(missing_ok=True)


# ---------- routes ----------
@app.get("/")
def index():
    for d in (BASE / "static", BASE):
        if (d / "index.html").exists():
            return send_from_directory(d, "index.html")
    return "index.html file repo me nahi mili. Use app.py ke saath upload karo.", 404


@app.post("/api/generate")
def generate():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not YT_RE.match(url):
        return jsonify(error="Sahi YouTube link daalo."), 400
    try:
        n = max(1, min(int(body.get("count", 5)), 12))
        lo = max(10, int(body.get("min", 30)))
        hi = min(120, int(body.get("max", 60)))
    except (TypeError, ValueError):
        return jsonify(error="Settings galat hain."), 400
    if hi <= lo:
        return jsonify(error="Length range galat hai."), 400

    purge_old()
    jid = uuid.uuid4().hex
    jobs[jid] = {"status": "running", "stage": "Shuru ho raha hai...", "progress": 0,
                 "clips": [], "plan": [], "duration": 0, "title": ""}
    threading.Thread(
        target=process,
        args=(jid, url, n, lo, hi, body.get("format", "vertical") == "vertical",
              bool(body.get("captions", True))),
        daemon=True,
    ).start()
    return jsonify(id=jid)


@app.get("/api/status/<jid>")
def status(jid):
    job = jobs.get(jid)
    if not job:
        return jsonify(error="Job nahi mila (server restart hua ho sakta hai)."), 404
    return jsonify(job)


@app.get("/clips/<jid>/<name>")
def clip_file(jid, name):
    if not re.fullmatch(r"[0-9a-f]{32}", jid):
        return "", 404
    return send_from_directory(DATA / jid / "clips", name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
