"""Read-only home video server: browse folders of videos and play them in the browser, HDR included.
Audio files (mp3, m4a, wav, flac, ogg, opus, aac) are listed and play too; subtitles and text
(srt, vtt, txt) open as a page to read, with a download link.

usage: python media_server.py [--port 8765] [--bind 0.0.0.0] [--lang uk|en] [folder ...]
  With no folders, serves the ones in config.json next to this file, or else ~/Videos.

Every subfolder becomes a page; video files get an HTML5 player and a download link. HTTP Range
is supported, so players can seek (Safari / iPhone will not play without it). Only files under the
configured folders are reachable (paths are resolved and checked), and nothing can be uploaded,
changed or deleted. HLG/HEVC files play in HDR in Safari (iPhone, iPad, Mac) and in Edge/Chrome on
Windows with HEVC hardware decoding.

Light version: if ffmpeg is installed, every video also offers a "light" copy for a phone on mobile
data or a slow VPN: at most 1280x720, SDR (HLG/PQ tone-mapped), H.264 + AAC, about 2-3 Mbit/s.
It is made on demand, once, on an NVIDIA card (NVENC) when one is free, else on the CPU, and kept
in a cache folder of limited size (least recently watched go first). The page shows the progress.

Python standard library only (ffmpeg is optional, for the light version). Meant for a home network
or a private VPN such as Tailscale; do not expose it to the internet: there is no login.
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
OPTS = {}   # per root: "only" = regex a file name must match, "flat" = no subfolders
VIDEO = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
         ".mkv": "video/x-matroska"}
AUDIO = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav", ".flac": "audio/flac",
         ".ogg": "audio/ogg", ".opus": "audio/ogg"}
TEXTS = {".srt": "application/x-subrip", ".vtt": "text/vtt", ".txt": "text/plain"}
MEDIA = {**VIDEO, **AUDIO, **TEXTS}
TEXT_MAX = 2 << 20   # bigger text files are only offered for download
CHUNK = 1 << 20
TEXT = {
    "uk": {"home": "Відео", "all": "Усі папки", "empty": "Тут немає відео, аудіо чи субтитрів.", "files": "відео",
           "folders": "папок", "download": "Завантажити файл", "nf": "Не знайдено", "gb": "ГБ", "mb": "МБ",
           "back": "Назад", "big": "Файл завеликий для перегляду, завантажте його.",
           "lite": "Легка версія (720p, менше трафіку)", "orig": "Оригінал", "making": "Готую легку версію",
           "queued": "У черзі", "wait": "Сторінка оновиться сама, коли версія буде готова.",
           "failed": "Не вдалося зробити легку версію", "retry": "Спробувати ще раз",
           "buffer": "Завантажено наперед", "lost": "немає зв'язку, пробую ще"},
    "en": {"home": "Videos", "all": "All folders", "empty": "No videos, audio or subtitles here.", "files": "videos",
           "folders": "folders", "download": "Download", "nf": "Not found", "gb": "GB", "mb": "MB",
           "back": "Back", "big": "Too big to show here; download it.",
           "lite": "Light version (720p, less data)", "orig": "Original", "making": "Making the light version",
           "queued": "Queued", "wait": "This page refreshes by itself when it is ready.",
           "failed": "Could not make the light version", "retry": "Try again",
           "buffer": "Loaded ahead", "lost": "no connection, retrying"},
}

LITE_VERSION = "v2"            # bump when the encoding changes, so old cache files are not served
LITE_LONG, LITE_SHORT = 1280, 720
LITE_SEG = 6                   # seconds per HLS segment
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HLS_JS = "https://cdn.jsdelivr.net/npm/hls.js@1.5.18/dist/hls.min.js"
# The light player: hls.js keeps up to 2 minutes loaded ahead and keeps retrying through a dropped
# connection (Chrome, Edge, Firefox, iPhone/iPad with iOS 17.1+); older Safari plays the HLS natively.
LITE_PLAYER = """(function(){
var v=document.getElementById('v'),src=$SRC,info=document.getElementById('buf');
function ahead(){var b=v.buffered,t=v.currentTime;for(var i=0;i<b.length;i++){
 if(b.start(i)<=t+0.5&&b.end(i)>=t)return b.end(i)-t;}return 0;}
var lost=0,loaded=false;
setInterval(function(){info.textContent=$BUF+': '+Math.round(ahead())+' s'+(Date.now()-lost<6000?' · '+$LOST:'');},1000);
var retry={maxNumRetry:1000,retryDelayMs:1000,maxRetryDelayMs:8000};
var pol={default:{maxTimeToFirstByteMs:20000,maxLoadTimeMs:120000,timeoutRetry:retry,errorRetry:retry}};
if(window.Hls&&Hls.isSupported()){
 var h=new Hls({startPosition:0,maxBufferLength:120,maxMaxBufferLength:180,maxBufferSize:120*1000*1000,
  backBufferLength:30,fragLoadPolicy:pol,playlistLoadPolicy:pol,manifestLoadPolicy:pol});
 h.on(Hls.Events.MANIFEST_PARSED,function(){loaded=true;});
 h.on(Hls.Events.ERROR,function(e,d){
  if(d.type===Hls.ErrorTypes.NETWORK_ERROR)lost=Date.now();
  if(!d.fatal)return;
  if(d.type===Hls.ErrorTypes.MEDIA_ERROR)h.recoverMediaError();
  else setTimeout(function(){if(loaded)h.startLoad(v.currentTime);else h.loadSource(src);},3000);});
 h.loadSource(src);h.attachMedia(v);
}else{v.src=src;}
})();"""
HDR_TRC = ("arib-std-b67", "smpte2084")
PLACEBO = ("libplacebo=w={w}:h={h}:tonemapping=bt.2390:colorspace=bt709:color_primaries=bt709:"
           "color_trc=bt709:range=tv:format=yuv420p")
ZSCALE = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
          "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,scale={w}:{h}")


class Lite:
    """Light versions as HLS: 6 s segments in a cache folder per video, made one video at a time by a
    worker thread. Playable as soon as the first segments exist (the encoder runs at about 4x real time,
    so it stays ahead of the viewer); the playlist gets #EXT-X-ENDLIST when the whole film is done."""

    def __init__(self, cache_dir, cap_bytes, gpu=0, ffmpeg=None, ffprobe=None):
        self.dir, self.cap, self.gpu = cache_dir, cap_bytes, gpu
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.ffprobe = ffprobe or shutil.which("ffprobe")
        self.ok = bool(self.ffmpeg and self.ffprobe)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = []            # source paths, in order
        self.jobs = {}             # cache folder -> {"state": queued|busy|failed, "pct": 0..100, "err": text}
        if self.ok:
            os.makedirs(self.dir, exist_ok=True)
            threading.Thread(target=self._worker, daemon=True).start()

    def path(self, src):
        """The cache folder of this video's light version (index.m3u8 + seg_00000.ts ...)."""
        st = os.stat(src)
        key = "%s|%d|%d|%s" % (os.path.normcase(src), st.st_size, int(st.st_mtime), LITE_VERSION)
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.basename(src))[0])[:40]
        return os.path.join(self.dir, "%s_%s" % (stem, hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]))

    @staticmethod
    def _playlist(out):
        try:
            with open(os.path.join(out, "index.m3u8"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def status(self, src, start=False, retry=False):
        """(state, pct, err, playable): state ready | queued | busy | failed | none; start=True queues it."""
        out = self.path(src)
        pl = self._playlist(out)
        if "#EXT-X-ENDLIST" in pl:
            try:
                os.utime(out)      # mtime = last watched, for the cache clean-up
            except OSError:
                pass
            return "ready", 100.0, "", True
        with self.lock:
            job = self.jobs.get(out)
            if job and job["state"] == "failed" and retry:
                del self.jobs[out]
                job = None
            if not job and start:
                shutil.rmtree(out, ignore_errors=True)      # left half-made by a stopped server
                job = self.jobs[out] = {"state": "queued", "pct": 0.0, "err": ""}
                self.queue.append(src)
                self.wake.set()
            if not job:
                return "none", 0.0, "", False
            playable = job["state"] == "busy" and pl.count("#EXTINF") >= 2
            return job["state"], job["pct"], job["err"], playable

    def file(self, src, name):
        """Path of index.m3u8 or a segment of this video's light version, or None."""
        if name != "index.m3u8" and not re.fullmatch(r"seg_\d{5}\.ts", name):
            return None
        p = os.path.join(self.path(src), name)
        return p if os.path.isfile(p) else None

    def _worker(self):
        while True:
            self.wake.wait()
            with self.lock:
                if not self.queue:
                    self.wake.clear()
                    continue
                src = self.queue.pop(0)
            out = self.path(src)
            with self.lock:
                self.jobs[out]["state"] = "busy"
            try:
                self._make(src, out)
                with self.lock:
                    self.jobs.pop(out, None)
            except Exception as e:          # keep the worker alive whatever happens
                shutil.rmtree(out, ignore_errors=True)
                with self.lock:
                    self.jobs[out] = {"state": "failed", "pct": 0.0, "err": str(e)[-400:]}
            self._trim()

    def _probe(self, src):
        r = subprocess.run([self.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", src],
                           capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        d = json.loads(r.stdout or "{}")
        v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
        if not v:
            raise RuntimeError("no video stream")
        w, h = int(v["width"]), int(v["height"])
        rot = int(float(v.get("tags", {}).get("rotate", 0) or 0))
        for sd in v.get("side_data_list", []):
            rot = int(float(sd.get("rotation", rot) or 0))
        if abs(rot) % 180 == 90:                # ffmpeg turns it upright before the filters
            w, h = h, w
        dur = float(d.get("format", {}).get("duration") or v.get("duration") or 0)
        audio = any(s.get("codec_type") == "audio" for s in d.get("streams", []))
        return w, h, dur, v.get("color_transfer") in HDR_TRC, audio

    def _encoders(self):
        """h264_nvenc on the chosen card if its encoder is under 75 % busy, else the other card; then the CPU."""
        out = []
        try:
            q = subprocess.run(["nvidia-smi", "--query-gpu=index,utilization.encoder", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout
            load = {int(a): int(b) for a, b in (ln.split(",") for ln in q.strip().splitlines())}
            for i in [self.gpu] + sorted(k for k in load if k != self.gpu):
                if i in load and load[i] < 75:
                    out.append(["-c:v", "h264_nvenc", "-gpu", str(i), "-preset", "p4", "-rc", "vbr", "-cq", "28",
                                "-b:v", "2500k", "-maxrate", "4000k", "-bufsize", "6000k", "-profile:v", "high",
                                "-forced-idr", "1"])
                    break
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        out.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-maxrate", "4000k", "-bufsize", "6000k",
                    "-profile:v", "high", "-sc_threshold", "0"])
        return out

    def _make(self, src, out):
        w, h, dur, hdr, audio = self._probe(src)
        s = min(1.0, LITE_LONG / max(w, h), LITE_SHORT / min(w, h))
        tw, th = max(2, int(w * s / 2) * 2), max(2, int(h * s / 2) * 2)
        filters = [PLACEBO, ZSCALE] if hdr else ["scale={w}:{h},format=yuv420p"]
        tries = [(f, e) for f in filters for e in self._encoders()]
        env = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID")     # NVENC -gpu N = nvidia-smi index N
        err = ""
        for vf, enc in tries:
            shutil.rmtree(out, ignore_errors=True)
            os.makedirs(out)
            sound = ["-c:a", "aac", "-b:a", "128k", "-ac", "2"] if audio else ["-an"]
            cmd = ([self.ffmpeg, "-v", "error", "-nostats", "-progress", "pipe:1", "-y", "-i", src,
                    "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf.format(w=tw, h=th)] + enc
                   + ["-force_key_frames", "expr:gte(t,n_forced*%d)" % LITE_SEG] + sound
                   + ["-f", "hls", "-hls_time", str(LITE_SEG), "-hls_playlist_type", "event",
                      "-hls_flags", "independent_segments+temp_file",
                      "-hls_segment_filename", os.path.join(out, "seg_%05d.ts"), os.path.join(out, "index.m3u8")])
            with tempfile.TemporaryFile() as errf:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True, env=env, creationflags=NO_WINDOW)
                for line in p.stdout:
                    if line.startswith("out_time_us=") and dur > 0:
                        try:
                            pct = min(99.0, int(line.split("=")[1]) / 1e6 / dur * 100)
                        except ValueError:
                            continue
                        with self.lock:
                            if out in self.jobs:
                                self.jobs[out]["pct"] = pct
                p.wait()
                errf.seek(0)
                err = errf.read().decode("utf-8", "replace").strip()
            if p.returncode == 0 and "#EXT-X-ENDLIST" in self._playlist(out):
                return
            with self.lock:
                if out in self.jobs:
                    self.jobs[out]["pct"] = 0.0
        raise RuntimeError(err or "ffmpeg failed")

    def _trim(self):
        dirs = []
        for f in os.listdir(self.dir):
            p = os.path.join(self.dir, f)
            if not os.path.isdir(p) or "#EXT-X-ENDLIST" not in self._playlist(p):
                continue           # only finished ones; one being made is never removed
            try:
                size = sum(e.stat().st_size for e in os.scandir(p))
                dirs.append((os.stat(p).st_mtime, size, p))
            except OSError:
                pass
        total = sum(s for _, s, _ in dirs)
        for _, s, p in sorted(dirs):
            if total <= self.cap:
                break
            shutil.rmtree(p, ignore_errors=True)
            total -= s


PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
:root {{ color-scheme: light dark; --bg:#f6f6f4; --fg:#1c1c1a; --mut:#6b6b66; --card:#fff; --line:#e2e2dc; --acc:#b4451f; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141413; --fg:#ececea; --mut:#9a9a94; --card:#1f1f1d; --line:#2e2e2b; --acc:#e07a52; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.45 -apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:960px; margin:0 auto; padding:16px; }}
h1 {{ font-size:22px; margin:8px 0 16px; overflow-wrap:anywhere; }}
a {{ color:var(--acc); text-decoration:none; }}
ul {{ list-style:none; padding:0; margin:0; }}
li {{ background:var(--card); border:1px solid var(--line); border-radius:10px; margin:8px 0; }}
li a {{ display:block; padding:12px 14px; overflow-wrap:anywhere; }} .meta {{ color:var(--mut); font-size:13px; }}
li.dir a::before {{ content:"\\1F4C1  "; }}
video {{ width:100%; max-height:80vh; background:#000; border-radius:8px; }} audio {{ width:100%; }}
li.aud a::before {{ content:"\\266A  "; color:var(--mut); }}
li.txt a::before {{ content:"\\1F4C4  "; }}
pre {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; white-space:pre-wrap;
      overflow-wrap:anywhere; font:14px/1.5 ui-monospace,Consolas,monospace; }}
.crumbs {{ color:var(--mut); font-size:14px; overflow-wrap:anywhere; }}
</style></head><body><main>{body}</main></body></html>"""


def load_roots(args_folders):
    """[(key, title, absolute path)]: from the command line, else config.json, else ~/Videos."""
    roots = [{"path": f} for f in args_folders]
    if not roots:
        try:
            with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
                roots = json.load(f).get("folders", [])
        except (OSError, ValueError):
            roots = []
    if not roots:
        roots = [{"path": os.path.expanduser("~/Videos")}]
    out, seen = [], set()
    for r in roots:
        path = os.path.realpath(os.path.expanduser(r["path"]))
        title = r.get("title") or os.path.basename(path) or path
        # URL key from the folder name (titles may be in any script)
        key = re.sub(r"[^A-Za-z0-9_-]+", "-", r.get("key") or os.path.basename(path)).strip("-").lower() or "root"
        while key in seen:
            key += "-"
        seen.add(key)
        out.append((key, title, path))
        OPTS[key] = {"only": re.compile(r["only"], re.I) if r.get("only") else None, "flat": bool(r.get("flat"))}
    return out


class Handler(BaseHTTPRequestHandler):
    roots, t, lang, hide, lite = [], TEXT["uk"], "uk", None, None

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # --- helpers -------------------------------------------------------------------------
    def send_page(self, title, body, code=200):
        data = PAGE.format(lang=self.lang, title=html.escape(title), body=body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def not_found(self):
        self.send_page(self.t["nf"], "<p>%s. <a href=\"/\">%s</a></p>" % (self.t["nf"], self.t["all"]), 404)

    def resolve(self, key, rel):
        """Absolute path for root key + relative parts, or None if it leaves the root."""
        root = next((r for r in self.roots if r[0] == key), None)
        if not root:
            return None, None
        full = os.path.realpath(os.path.join(root[2], *rel))
        if full != root[2] and not full.startswith(root[2] + os.sep):
            return None, None
        return root, full

    def visible(self, name):
        return not name.startswith(".") and not (self.hide and self.hide.search(name))

    def human(self, n):
        return "%.2f %s" % (n / 1e9, self.t["gb"]) if n > 1e9 else "%.0f %s" % (n / 1e6, self.t["mb"])

    def href(self, key, rel, *extra):
        return "/" + "/".join(urllib.parse.quote(p) for p in [key] + list(extra) + list(rel))

    # --- routes: /  /<key>/<dir...>  /<key>/play/<file...>  /<key>/file/<file...> ----------------
    #     light version of a video: /<key>/lite/<file...> (page), /<key>/litehls/<file...>?s=<name> (HLS files)
    def do_GET(self):
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
        if not parts:
            return self.index()
        mode, rel = "dir", parts[1:]
        if rel and rel[0] in ("play", "file", "lite", "litehls"):
            mode, rel = rel[0], rel[1:]
        if mode in ("lite", "litehls"):
            if not (self.lite and self.lite.ok and rel and os.path.splitext(rel[-1])[1].lower() in VIDEO):
                return self.not_found()
        if any(p in ("..", ".") for p in rel):
            return self.not_found()
        root, full = self.resolve(parts[0], rel)
        if not root:
            return self.not_found()
        if mode == "dir" and os.path.isdir(full):
            return self.folder(root, rel, full)
        name = os.path.basename(full)
        only = OPTS.get(root[0], {}).get("only")
        if mode in ("play", "file", "lite", "litehls") and os.path.isfile(full) \
                and os.path.splitext(full)[1].lower() in MEDIA and self.visible(name) and not (only and not only.search(name)):
            if mode == "lite":
                return self.lite_page(root, rel, full)
            if mode == "litehls":
                return self.lite_file(root, rel, full)
            return self.player(root, rel) if mode == "play" else self.send_file(full)
        return self.not_found()

    do_HEAD = do_GET

    def index(self):
        items = []
        for key, title, path in self.roots:
            items.append('<li class="dir"><a href="/%s">%s<div class="meta">%s</div></a></li>'
                         % (urllib.parse.quote(key), html.escape(title), html.escape(path)))
        self.send_page(self.t["home"], "<h1>%s</h1><ul>%s</ul>" % (self.t["home"], "".join(items)))

    def folder(self, root, rel, full):
        dirs, files = [], []
        try:
            entries = list(os.scandir(full))
        except OSError:
            entries = []
        for e in entries:
            if not self.visible(e.name):
                continue
            opt = OPTS.get(root[0], {})
            if e.is_dir():
                if not opt.get("flat"):
                    dirs.append(e)
            elif os.path.splitext(e.name)[1].lower() in MEDIA and not (opt.get("only") and not opt["only"].search(e.name)):
                files.append(e)
        dirs.sort(key=lambda e: e.name.lower(), reverse=True)        # dated names: newest first
        files.sort(key=lambda e: -e.stat().st_mtime)
        rows = ['<li class="dir"><a href="%s">%s</a></li>' % (self.href(root[0], rel + [d.name]), html.escape(d.name))
                for d in dirs]
        for f in files:
            st = f.stat()
            ext = os.path.splitext(f.name)[1].lower()
            cls = ' class="aud"' if ext in AUDIO else ' class="txt"' if ext in TEXTS else ""
            rows.append('<li%s><a href="%s">%s<div class="meta">%s · %s</div></a></li>'
                        % (cls, self.href(root[0], rel + [f.name], "play"), html.escape(f.name), self.human(st.st_size),
                           datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")))
        crumbs = ['<a href="/">%s</a>' % self.t["all"], '<a href="/%s">%s</a>' % (root[0], html.escape(root[1]))]
        for i, p in enumerate(rel):
            crumbs.append('<a href="%s">%s</a>' % (self.href(root[0], rel[:i + 1]), html.escape(p)))
        title = rel[-1] if rel else root[1]
        body = '<p class="crumbs">%s</p><h1>%s</h1><ul>%s</ul>' % (
            " / ".join(crumbs), html.escape(title), "".join(rows) or "<p>%s</p>" % self.t["empty"])
        self.send_page(title, body)

    def player(self, root, rel):
        src = self.href(root[0], rel, "file")
        if os.path.splitext(rel[-1])[1].lower() in TEXTS:
            return self.text_page(root, rel, src)
        tag ="audio" if os.path.splitext(rel[-1])[1].lower() in AUDIO else "video"
        lite = ""
        if tag == "video" and self.lite and self.lite.ok:
            lite = ' · <a href="%s">%s</a>' % (self.href(root[0], rel, "lite"), self.t["lite"])
        body = ('<p class="crumbs"><a href="%s">&larr; %s</a></p><h1>%s</h1>'
                '<%s controls playsinline preload="metadata" src="%s"></%s>'
                '<p class="meta"><a href="%s" download>%s</a>%s</p>'
                % (self.href(root[0], rel[:-1]), html.escape(rel[-2] if len(rel) > 1 else root[1]),
                   html.escape(rel[-1]), tag, src, tag, src, self.t["download"], lite))
        self.send_page(rel[-1], body)

    def lite_page(self, root, rel, full):
        """The light version's player, or its progress (the page reloads itself until it is ready)."""
        retry = "retry=1" in urllib.parse.urlsplit(self.path).query
        state, pct, err, playable = self.lite.status(full, start=True, retry=retry)
        head = ('<p class="crumbs"><a href="%s">&larr; %s</a></p><h1>%s</h1>'
                % (self.href(root[0], rel[:-1]), html.escape(rel[-2] if len(rel) > 1 else root[1]), html.escape(rel[-1])))
        orig = '<a href="%s">%s</a>' % (self.href(root[0], rel, "play"), self.t["orig"])
        if playable:
            src = self.href(root[0], rel, "litehls") + "?s=index.m3u8"
            rest = "" if state == "ready" else " · %s: %.0f %%" % (self.t["making"], pct)
            body = (head + '<video id="v" controls playsinline disableremoteplayback preload="auto"></video>'
                    '<p class="meta"><span id="buf"></span>%s</p><p class="meta">%s</p>'
                    '<script src="%s"></script><script>%s</script>'
                    % (rest, orig, HLS_JS, LITE_PLAYER.replace("$SRC", json.dumps(src))
                       .replace("$BUF", json.dumps(self.t["buffer"])).replace("$LOST", json.dumps(self.t["lost"]))))
        elif state == "failed":
            body = (head + '<p>%s.</p><pre>%s</pre><p class="meta"><a href="%s?retry=1">%s</a> · %s</p>'
                    % (self.t["failed"], html.escape(err), self.href(root[0], rel, "lite"), self.t["retry"], orig))
        else:
            what = self.t["queued"] if state == "queued" else "%s: %.0f %%" % (self.t["making"], pct)
            body = (head + '<p>%s</p><p class="meta">%s</p><p class="meta">%s</p>'
                    '<script>setTimeout(function(){location.replace(location.pathname)},3000)</script>'
                    % (html.escape(what), self.t["wait"], orig))
        self.send_page(rel[-1], body)

    def lite_file(self, root, rel, full):
        """index.m3u8 (segment names turned into full links) or one .ts segment of the light version."""
        name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("s", [""])[0]
        fpath = self.lite.file(full, name)
        if not fpath:
            return self.not_found()
        if name.endswith(".ts"):
            return self.send_file(fpath, "video/mp2t")
        base = self.href(root[0], rel, "litehls") + "?s="
        lines = []
        for ln in Lite._playlist(os.path.dirname(fpath)).splitlines():
            if ln.startswith("#EXTM3U"):
                lines += [ln, "#EXT-X-START:TIME-OFFSET=0"]     # from the start, also while it is still being made
            elif ln and not ln.startswith("#"):
                lines.append(base + ln.strip())
            else:
                lines.append(ln)
        data = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def text_page(self, root, rel, src):
        """Subtitles / text: the contents on the page, and a link that downloads the file."""
        fpath = self.resolve(root[0], rel)[1]
        if os.path.getsize(fpath) > TEXT_MAX:
            shown = "<p>%s</p>" % self.t["big"]
        else:
            with open(fpath, "rb") as f:
                raw = f.read()
            try:
                txt = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                txt = raw.decode("cp1251", errors="replace")     # old Windows text files
            shown = "<pre>%s</pre>" % html.escape(txt)
        body = ('<p class="crumbs"><a href="%s">&larr; %s</a></p><h1>%s</h1>'
                '<p class="meta"><a href="%s?dl=1" download>%s</a></p>%s'
                % (self.href(root[0], rel[:-1]), html.escape(rel[-2] if len(rel) > 1 else root[1]),
                   html.escape(rel[-1]), src, self.t["download"], shown))
        self.send_page(rel[-1], body)

    def send_file(self, fpath, ctype=None):
        size = os.path.getsize(fpath)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        ext = os.path.splitext(fpath)[1].lower()
        self.send_header("Content-Type", ctype or MEDIA.get(ext, "application/octet-stream")
                         + ("; charset=utf-8" if ext in TEXTS else ""))
        if ext in TEXTS and "dl=1" in urllib.parse.urlsplit(self.path).query:
            # a real file save, also where the browser ignores the download attribute
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s"
                             % urllib.parse.quote(os.path.basename(fpath)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(fpath, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # players drop connections when seeking


def main():
    if sys.stdout is None or sys.stderr is None:      # pythonw (no console): log to a file instead
        log = open(os.path.join(HERE, "media_server.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    ap = argparse.ArgumentParser(description="Read-only home video server")
    ap.add_argument("folders", nargs="*", help="folders to serve (default: config.json, else ~/Videos)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bind", default=None, help="address to listen on (default 0.0.0.0: all interfaces)")
    ap.add_argument("--lang", choices=sorted(TEXT), default=None)
    ap.add_argument("--no-lite", action="store_true", help="do not offer light versions (no ffmpeg use)")
    a = ap.parse_args()
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    Handler.roots = load_roots(a.folders)
    Handler.lang = a.lang or cfg.get("lang", "uk")
    Handler.t = TEXT[Handler.lang]
    if cfg.get("hide"):
        Handler.hide = re.compile(cfg["hide"], re.I)     # e.g. "_raw\\.|^x_" to skip intermediate renders
    port, bind = a.port or cfg.get("port", 8765), a.bind or cfg.get("bind", "0.0.0.0")
    if not a.no_lite and cfg.get("lite", True):
        Handler.lite = Lite(os.path.expanduser(cfg.get("lite_cache") or os.path.join(HERE, "cache")),
                            int(float(cfg.get("lite_cache_gb", 20)) * (1 << 30)), gpu=int(cfg.get("lite_gpu", 0)),
                            ffmpeg=cfg.get("ffmpeg"), ffprobe=cfg.get("ffprobe"))
        print("light versions: %s" % ("cache " + Handler.lite.dir if Handler.lite.ok else "off (ffmpeg not found)"))
    ThreadingHTTPServer.daemon_threads = True
    for key, title, path in Handler.roots:
        print("  /%-20s %s" % (key, path))
    print("serving on http://%s:%d" % (bind, port))
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
