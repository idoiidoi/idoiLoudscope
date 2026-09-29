#!/usr/bin/env python3
"""Loudscope — check loudness and nudge gain from the Finder right-click menu.

Usage:
  loudscope.py install                 Install the Finder Quick Actions (re-run to update)
  loudscope.py uninstall               Remove everything Loudscope installed
  loudscope.py doctor                  Check dependencies and the installation
  loudscope.py analyze FILE...         Analyze; show a summary dialog (graph on request)
      --html / --text                  Open the HTML report directly / print the summary
  loudscope.py gain FILE...            Pick a gain in a dialog and export a new file
  loudscope.py gain --db 3 FILE...     No dialog: +3 dB
  loudscope.py gain --lufs -14 FILE... No dialog: match -14 LUFS integrated
  loudscope.py gain --tp -1 FILE...    No dialog: set true peak to -1 dBTP
      --limit / --no-limit             What to do if true peak would exceed -1 dBTP
                                       (default: ask)
      --quiet                          No dialogs, notifications or browser

Requires macOS, Python 3.9+ and ffmpeg (brew install ffmpeg).
"""
import array
import html
import json
import math
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

VERSION = "0.1.0"
TP_CEILING = -1.0          # true-peak ceiling for exports (dBTP)
LIMIT_MARGIN = 0.3         # limiter threshold sits this far below the ceiling (dB)
WAVE_BUCKETS = 1600
APP_DIR = os.path.expanduser("~/Library/Application Support/Loudscope")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CACHE_DIR = os.path.expanduser("~/Library/Caches/Loudscope")
LOG_PATH = os.path.join(CACHE_DIR, "loudscope.log")
SERVICES_DIR = os.path.expanduser("~/Library/Services")
QUICK_ACTIONS = [
    ("Loudscope: Analyze Loudness", "analyze"),
    ("Loudscope: Change Gain…", "gain"),
]
QUIET = False


# ---------------------------------------------------------------- environment

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def setup_path():
    """Finder Quick Actions run with a bare PATH, so add the ffmpeg dir found at install time."""
    extra = [load_config().get("ffmpeg_dir"), "/opt/homebrew/bin", "/usr/local/bin"]
    os.environ["PATH"] = ":".join([d for d in extra if d] + [os.environ.get("PATH", "/usr/bin:/bin")])


setup_path()


def log(msg):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def osa(script, *args):
    """Run AppleScript and return stdout, or None if cancelled / failed."""
    p = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def notify(title, text):
    if QUIET:
        return
    osa('on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run',
        title, text)


def alert(title, text):
    if QUIET:
        print("ERROR:", title, "-", text, file=sys.stderr)
        return
    osa('on run argv\nactivate\ndisplay alert (item 1 of argv) message (item 2 of argv) as critical\nend run',
        title, text)


def fnum(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def fmt_db(v, digits=1, plus=False):
    if v is None or v == float("-inf"):
        return "−∞"
    s = ("%+.*f" if plus else "%.*f") % (digits, v)
    return s.replace("-", "−")


def fmt_time(t):
    t = max(0, t)
    if t >= 3600:
        return "%d:%02d:%02d" % (t // 3600, (t % 3600) // 60, t % 60)
    return "%d:%02d" % (t // 60, t % 60)


def db_to_lin(db):
    return 10 ** (db / 20.0)


_ENCODERS = None


def has_encoder(name):
    global _ENCODERS
    if _ENCODERS is None:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
        _ENCODERS = set(l.split()[1] for l in p.stdout.splitlines()
                        if len(l.split()) > 1 and l.startswith(" A"))
    return name in _ENCODERS


# ---------------------------------------------------------------- analysis

def probe(path):
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate,channels,bits_per_sample,"
         "bits_per_raw_sample,sample_fmt,bit_rate:format=duration,format_name,bit_rate",
         "-of", "json", path], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffprobe could not read the file: " + p.stderr.strip()[-300:])
    j = json.loads(p.stdout)
    if not j.get("streams"):
        raise RuntimeError("No audio stream found")
    s, f = j["streams"][0], j.get("format", {})
    return {
        "codec": s.get("codec_name", "?"),
        "sample_fmt": s.get("sample_fmt", ""),
        "sr": int(s.get("sample_rate") or 0),
        "ch": int(s.get("channels") or 0),
        "bits": int(s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 0),
        "bit_rate": int(s.get("bit_rate") or f.get("bit_rate") or 0),
        "duration": float(f.get("duration") or 0),
        "format": f.get("format_name", ""),
    }


FRAME_RE = re.compile(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(\S+)\s+S:\s*(\S+).*?FTPK:\s*(.*?)dBFS")


def loudness(path, frames=True):
    """Integrated / LRA / true peak / sample peak via ffmpeg ebur128, plus M and S every 100 ms."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0",
           "-af", "ebur128=peak=true+sample" + (":framelog=info" if frames else ""),
           "-f", "null", "-"]
    p = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         text=True, errors="replace")
    t, m, s, fp = [], [], [], []
    summary, section, in_summary = {}, None, False
    for line in p.stderr:
        if not in_summary:
            if "Summary:" in line:
                in_summary = True
                continue
            if frames and " t:" in line:
                mo = FRAME_RE.search(line)
                if mo:
                    t.append(float(mo.group(1)))
                    m.append(fnum(mo.group(2)))
                    s.append(fnum(mo.group(3)))
                    pk = [x for x in (fnum(v) for v in mo.group(4).split()) if x is not None]
                    fp.append(max(pk) if pk else None)
            continue
        l = line.strip()
        if l.endswith(":") and not l.startswith(("I:", "LRA:", "Peak:")):
            section = l[:-1]
            continue
        mo = re.match(r"([A-Za-z ]+):\s*(\S+)", l)
        if mo:
            key, val = mo.group(1), fnum(mo.group(2))
            if key == "I":
                summary["I"] = val
            elif key == "LRA":
                summary["LRA"] = val
            elif key == "Peak" and section == "True peak":
                summary["TP"] = val
            elif key == "Peak" and section == "Sample peak":
                summary["SP"] = val
    p.wait()
    if p.returncode != 0 or "I" not in summary:
        raise RuntimeError("Loudness analysis failed")
    summary.update(t=t, M=m, S=s, FTPK=fp)
    return summary


def waveform(path, info, buckets=WAVE_BUCKETS):
    """Min/max envelope across all channels, read at the native sample rate."""
    ch = max(1, info["ch"])
    total = max(1, int(info["duration"] * info["sr"]))
    per = max(1, math.ceil(total / buckets))
    nbytes = per * ch * 4
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0",
                          "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    lo, hi, clip = [], [], []

    def add(buf):
        a = array.array("f")
        a.frombytes(buf[: len(buf) // 4 * 4])
        mn, mx = min(a), max(a)
        lo.append(mn)
        hi.append(mx)
        clip.append(1 if (mx >= 0.9999 or mn <= -0.9999) else 0)

    buf = b""
    while True:
        chunk = p.stdout.read(nbytes - len(buf))
        if not chunk:
            break
        buf += chunk
        if len(buf) >= nbytes:
            add(buf)
            buf = b""
    if len(buf) >= 4:
        add(buf)
    p.wait()
    return {"lo": lo, "hi": hi, "clip": clip}


def analyze_file(path):
    info = probe(path)
    res = {}

    def run_wave():
        try:
            res["wave"] = waveform(path, info)
        except Exception as e:  # numbers are still useful without the waveform
            log("waveform failed: %r" % e)
            res["wave"] = None

    th = threading.Thread(target=run_wave)
    th.start()
    loud = loudness(path)
    th.join()
    return {"path": path, "info": info, "loud": loud, "wave": res["wave"]}


# ---------------------------------------------------------------- report

CSS = """
:root { --fg:#1a1a1a; --sub:#6b6b6b; --line:#e4e4e4; --accent:#1f5fd6; --m:#9bb6ea;
        --wave:#3d4a5c; --warn:#c2410c; --ok:#15803d; }
* { box-sizing:border-box; }
html, body { background:#FFFFFF; color:var(--fg); margin:0; }
body { font:14px/1.5 -apple-system, "Helvetica Neue", sans-serif;
       padding:28px 32px 48px; max-width:1280px; margin:0 auto; }
section + section { margin-top:56px; padding-top:32px; border-top:1px solid var(--line); }
h1 { font-size:20px; margin:0 0 2px; font-weight:600; word-break:break-all; }
.meta { color:var(--sub); font-size:12.5px; margin-bottom:20px; }
.meta code { font:11.5px ui-monospace, Menlo, monospace; word-break:break-all; }
.stats { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px; }
.stat { border:1px solid var(--line); border-radius:8px; padding:10px 14px; }
.stat .k { font-size:11.5px; color:var(--sub); }
.stat .v { font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-0.01em; }
.stat .v small { font-size:13px; font-weight:500; color:var(--sub); margin-left:3px; }
.stat.warn .v { color:var(--warn); }
h2 { font-size:13px; font-weight:600; color:var(--sub); margin:26px 0 6px; letter-spacing:.02em; }
.chart { position:relative; }
.chart svg { width:100%; height:auto; display:block; overflow:visible; }
.chart text { font:10.5px -apple-system, sans-serif; fill:var(--sub); }
.tip { position:absolute; top:0; pointer-events:none; background:#1a1a1a; color:#fff; font-size:11.5px;
       padding:3px 7px; border-radius:4px; white-space:nowrap; display:none;
       font-variant-numeric:tabular-nums; transform:translateX(-50%); }
table { border-collapse:collapse; font-variant-numeric:tabular-nums; font-size:13px; }
th, td { text-align:right; padding:5px 14px; border-bottom:1px solid var(--line); }
th:first-child, td:first-child { text-align:left; padding-left:0; }
th { font-weight:500; color:var(--sub); font-size:12px; }
td.warn { color:var(--warn); }
td.ok { color:var(--ok); }
.note { color:var(--sub); font-size:12px; margin-top:8px; }
.err { color:var(--warn); }
"""

JS = """
document.querySelectorAll('.chart[data-series]').forEach(function (box) {
  var d = JSON.parse(box.getAttribute('data-series'));
  var svg = box.querySelector('svg'), tip = box.querySelector('.tip'), cur = svg.querySelector('.cursor');
  var L = +box.dataset.l, W = +box.dataset.w, VB = +box.dataset.vb;
  function fmt(t){ t=Math.max(0,t); var m=Math.floor(t/60), s=Math.floor(t%60); return m+':'+(s<10?'0':'')+s; }
  function f(v){ return v==null ? '—' : v.toFixed(1).replace('-', '−'); }
  svg.addEventListener('mousemove', function (e) {
    var r = svg.getBoundingClientRect(), x = (e.clientX - r.left) / r.width * VB;
    if (x < L || x > L + W) { tip.style.display='none'; cur.style.display='none'; return; }
    var t = (x - L) / W * d.dur, i = Math.min(d.t.length-1, Math.max(0, Math.round(t / 0.1)));
    cur.setAttribute('x1', x); cur.setAttribute('x2', x); cur.style.display='';
    tip.innerHTML = fmt(t) + '&nbsp;&nbsp;S ' + f(d.S[i]) + '&nbsp;&nbsp;M ' + f(d.M[i]) +
      ' LUFS&nbsp;&nbsp;TP ' + f(d.P[i]) + ' dB';
    tip.style.left = (x / VB * 100) + '%'; tip.style.display='block';
  });
  svg.addEventListener('mouseleave', function(){ tip.style.display='none'; cur.style.display='none'; });
});
"""

VB_W, PAD_L, PAD_R = 1200, 46, 8
PLOT_W = VB_W - PAD_L - PAD_R


def time_ticks(dur):
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if dur / step <= 12:
            break
    t, out = 0, []
    while t <= dur + 1e-6:
        out.append(t)
        t += step
    return out


def x_of(t, dur):
    return PAD_L + (t / dur) * PLOT_W if dur > 0 else PAD_L


def hline(y, stroke, extra=""):
    return '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" %s/>' % (PAD_L, VB_W - PAD_R, y, y, stroke, extra)


def svg_wave(wave, dur):
    H, top, h = 170, 6, 150
    mid = top + h / 2.0
    n = len(wave["hi"])
    if n == 0:
        return ""
    xs = [PAD_L + (i + 0.5) / n * PLOT_W for i in range(n)]
    up = " ".join("%.1f,%.1f" % (x, mid - max(0.0, min(1.0, v)) * h / 2) for x, v in zip(xs, wave["hi"]))
    dn = " ".join("%.1f,%.1f" % (x, mid - max(-1.0, min(0.0, v)) * h / 2)
                  for x, v in zip(reversed(xs), reversed(wave["lo"])))
    g = []
    for db in (0, -6, -12):
        a = db_to_lin(db)
        g.append(hline(mid - a * h / 2, "#ececec") + hline(mid + a * h / 2, "#ececec"))
        g.append('<text x="%d" y="%.1f" text-anchor="end">%s</text>' % (PAD_L - 6, mid - a * h / 2 + 3.5, fmt_db(db, 0)))
    g.append(hline(mid, "#d4d4d4"))
    clips = "".join('<rect x="%.1f" y="%d" width="1.5" height="%d" fill="#c2410c"/>' % (xs[i] - .75, top, h)
                    for i, c in enumerate(wave["clip"]) if c)
    for t in time_ticks(dur):
        g.append('<text x="%.1f" y="%d" text-anchor="middle">%s</text>' % (x_of(t, dur), H - 1, fmt_time(t)))
    return ('<svg viewBox="0 0 %d %d">%s<polygon points="%s %s" fill="var(--wave)"/>%s</svg>'
            % (VB_W, H, "".join(g), up, dn, clips))


def svg_loud(loud, dur, target):
    H, top, h = 250, 8, 220
    lo_db, hi_db = -50.0, 0.0

    def y(v):
        v = max(lo_db, min(hi_db, v))
        return top + (hi_db - v) / (hi_db - lo_db) * h

    g = []
    for db in range(0, -51, -10):
        g.append(hline(y(db), "#ececec"))
        g.append('<text x="%d" y="%.1f" text-anchor="end">%s</text>' % (PAD_L - 6, y(db) + 3.5, fmt_db(db, 0)))
    for t in time_ticks(dur):
        g.append('<text x="%.1f" y="%d" text-anchor="middle">%s</text>' % (x_of(t, dur), H - 1, fmt_time(t)))

    def path(vals, attrs):
        segs, cur = [], []
        for t, v in zip(loud["t"], vals):
            if v is None or v < -70:
                if cur:
                    segs.append(cur)
                cur = []
                continue
            cur.append("%.1f,%.1f" % (x_of(t, dur), y(v)))
        if cur:
            segs.append(cur)
        return "".join('<polyline points="%s" %s/>' % (" ".join(sg), attrs) for sg in segs)

    g.append(path(loud["M"], 'fill="none" stroke="var(--m)" stroke-width="1"'))
    g.append(path(loud["S"], 'fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"'))
    I = loud.get("I")
    if target is not None:
        g.append(hline(y(target), "#888", 'stroke-dasharray="5 4"'))
        g.append('<text x="%d" y="%.1f" text-anchor="end" style="fill:#888">ref %s</text>'
                 % (VB_W - PAD_R - 4, y(target) - 4, fmt_db(target, 0)))
    if I is not None and I > -70:
        g.append(hline(y(I), "#1a1a1a", 'stroke-width="1.2"'))
        g.append('<text x="%d" y="%.1f" style="fill:#1a1a1a">I %s LUFS</text>' % (PAD_L + 6, y(I) - 5, fmt_db(I)))
    g.append('<line class="cursor" y1="%d" y2="%d" stroke="#1a1a1a" stroke-width="1" style="display:none"/>'
             % (top, top + h))
    return '<svg viewBox="0 0 %d %d">%s</svg>' % (VB_W, H, "".join(g))


def gain_rows(loud):
    I, TP = loud.get("I"), loud.get("TP")
    rows = []
    for label, tgt in (("−14 LUFS (Spotify / YouTube)", -14.0), ("−16 LUFS (Apple Music)", -16.0),
                       ("−23 LUFS (EBU R128 broadcast)", -23.0)):
        if I is None or I <= -70:
            continue
        g = tgt - I
        tp = None if TP is None else TP + g
        over = tp is not None and tp > TP_CEILING
        rows.append('<tr><td>%s</td><td>%s dB</td><td class="%s">%s dBTP%s</td></tr>'
                    % (label, fmt_db(g, 1, True), "warn" if over else "ok", fmt_db(tp),
                       " (needs limiter)" if over else ""))
    if TP is not None and TP != float("-inf"):
        g = TP_CEILING - TP
        rows.append('<tr><td>True peak to %s dBTP</td><td>%s dB</td><td>%s LUFS</td></tr>'
                    % (fmt_db(TP_CEILING), fmt_db(g, 1, True), fmt_db((I or 0) + g)))
    return rows


def render_section(r):
    path, info = r["path"], r["info"]
    name = html.escape(os.path.basename(path))
    if "error" in r:
        return '<section><h1>%s</h1><p class="err">%s</p></section>' % (name, html.escape(r["error"]))
    loud, wave = r["loud"], r["wave"]
    dur = info["duration"] or (loud["t"][-1] if loud["t"] else 0)
    fmt_parts = [info["codec"], "%g kHz" % (info["sr"] / 1000.0), "%d ch" % info["ch"]]
    if info["bits"] and info["codec"].startswith(("pcm", "flac", "alac")):
        fmt_parts.insert(1, "%d-bit" % info["bits"])
    elif info["bit_rate"]:
        fmt_parts.append("%d kbps" % round(info["bit_rate"] / 1000))
    fmt_parts.append(fmt_time(dur))

    I, LRA, TP, SP = loud.get("I"), loud.get("LRA"), loud.get("TP"), loud.get("SP")
    mvals = [v for v in loud["M"] if v is not None]
    svals = [v for v in loud["S"] if v is not None]

    def stat(k, v, unit, warn=False):
        return '<div class="stat%s"><div class="k">%s</div><div class="v">%s<small>%s</small></div></div>' % (
            " warn" if warn else "", k, v, unit)

    stats = "".join([
        stat("Integrated", fmt_db(I), "LUFS"),
        stat("Loudness Range", fmt_db(LRA), "LU"),
        stat("True Peak", fmt_db(TP), "dBTP", TP is not None and TP > TP_CEILING),
        stat("Sample Peak", fmt_db(SP), "dBFS", SP is not None and SP >= -0.01),
        stat("Max Short-term", fmt_db(max(svals) if svals else None), "LUFS"),
        stat("Max Momentary", fmt_db(max(mvals) if mvals else None), "LUFS"),
    ])
    series = json.dumps({
        "dur": dur, "t": loud["t"],
        "S": [None if v is None or v < -70 else round(v, 1) for v in loud["S"]],
        "M": [None if v is None or v < -70 else round(v, 1) for v in loud["M"]],
        "P": [None if v is None or v < -200 else round(v, 1) for v in loud["FTPK"]],
    }, separators=(",", ":"))
    clip_n = sum(wave["clip"]) if wave else 0
    wave_html = svg_wave(wave, dur) if wave else '<p class="note">Waveform unavailable.</p>'
    return """<section>
<h1>%s</h1>
<div class="meta">%s&nbsp;&nbsp;<code>%s</code></div>
<div class="stats">%s</div>
<h2>Waveform%s</h2>
<div class="chart">%s</div>
<h2>Loudness over time (thick: short-term 3 s, thin: momentary 400 ms)</h2>
<div class="chart" data-series='%s' data-l="%d" data-w="%d" data-vb="%d">%s<div class="tip"></div></div>
<h2>Gain suggestions</h2>
<table><tr><th>Target</th><th>Gain</th><th>Resulting true peak</th></tr>%s</table>
<p class="note">Ceiling for warnings: %s dBTP. Values are rounded to 0.1 dB by ffmpeg's ebur128.</p>
</section>""" % (
        name, " / ".join(fmt_parts), html.escape(os.path.dirname(path)), stats,
        (" (%d regions reach 0 dBFS, marked red)" % clip_n) if clip_n else "", wave_html,
        html.escape(series, quote=True), PAD_L, PLOT_W, VB_W, svg_loud(loud, dur, -14.0),
        "".join(gain_rows(loud)), fmt_db(TP_CEILING))


def write_report(results):
    os.makedirs(CACHE_DIR, exist_ok=True)
    title = os.path.basename(results[0]["path"]) + (" + %d more" % (len(results) - 1) if len(results) > 1 else "")
    body = "".join(render_section(r) for r in results)
    doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width, initial-scale=1">'
           '<title>%s — Loudscope</title><style>%s</style></head><body>%s<script>%s</script></body></html>'
           % (html.escape(title), CSS, body, JS))
    fd, out = tempfile.mkstemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), suffix=".html", dir=CACHE_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(doc)
    reports = sorted(x for x in os.listdir(CACHE_DIR) if x.endswith(".html"))
    for old in reports[:-30]:  # keep the latest 30
        try:
            os.remove(os.path.join(CACHE_DIR, old))
        except OSError:
            pass
    return out


def summary_line(loud):
    """One line for the Finder comment (plain ASCII minus so Spotlight search works)."""
    return "Loudscope: I %s LUFS · LRA %s LU · TP %s dBTP · SP %s dBFS" % tuple(
        fmt_db(loud.get(k)).replace("−", "-") for k in ("I", "LRA", "TP", "SP"))


FINDER_COMMENT_SCRIPT = '''on run argv
	repeat with i from 1 to (count of argv) by 2
		set f to POSIX file (item i of argv) as alias
		set newLine to item (i + 1) of argv
		tell application "Finder"
			set old to comment of f
			set kept to {}
			repeat with p in paragraphs of old
				if p does not start with "Loudscope:" and (length of p) > 0 then set end of kept to (p as text)
			end repeat
			set end of kept to newLine
			set AppleScript's text item delimiters to linefeed
			set comment of f to (kept as text)
			set AppleScript's text item delimiters to ""
			try
				update f
			end try
		end tell
	end repeat
end run'''


def write_finder_comments(pairs):
    """Put the loudness summary into each file's Finder comment (shown in Get Info, ⌘I)."""
    if not pairs or not load_config().get("finder_comment", True):
        return
    args = []
    for path, loud in pairs:
        args += [os.path.abspath(path), summary_line(loud)]
    p = subprocess.run(["osascript", "-e", FINDER_COMMENT_SCRIPT] + args, capture_output=True, text=True)
    if p.returncode != 0:
        log("finder comment failed: " + p.stderr.strip())


def dialog_text(r):
    name = os.path.basename(r["path"])
    if "error" in r:
        return "%s\n%s" % (name, r["error"])
    loud = r["loud"]
    I, TP = loud.get("I"), loud.get("TP")
    svals = [v for v in loud["S"] if v is not None]
    lines = [name,
             "Integrated  %s LUFS" % fmt_db(I),
             "Range  %s LU" % fmt_db(loud.get("LRA")),
             "True Peak  %s dBTP" % fmt_db(TP),
             "Sample Peak  %s dBFS" % fmt_db(loud.get("SP")),
             "Max Short-term  %s LUFS" % fmt_db(max(svals) if svals else None)]
    if I is not None and I > -70:
        g = -14.0 - I
        tp = None if TP is None else TP + g
        lines.append("To −14 LUFS: %s dB → TP %s dBTP%s" % (
            fmt_db(g, 1, True), fmt_db(tp), " (needs limiter)" if tp is not None and tp > TP_CEILING else ""))
    return "\n".join(lines)


def cmd_analyze(paths, mode="dialog"):
    results = []
    for p in paths:
        try:
            results.append(analyze_file(p))
        except Exception as e:
            log("analyze %s: %r" % (p, e))
            results.append({"path": p, "info": {}, "error": str(e)})
    ok = [r for r in results if "error" not in r]
    if not QUIET:
        write_finder_comments([(r["path"], r["loud"]) for r in ok])
    if QUIET or mode == "text":
        print("\n\n".join(dialog_text(r) for r in results))
        return 0 if ok else 1
    if mode == "html":
        subprocess.run(["open", write_report(results)])
        return 0
    text = "\n\n".join(dialog_text(r) for r in results)
    ans = osa('on run argv\nactivate\nset r to display dialog (item 1 of argv) with title "Loudscope" '
              'buttons {"Change Gain…", "Show Graph", "OK"} default button 3 cancel button 3\n'
              'return button returned of r\nend run', text)
    if ans == "Show Graph":
        subprocess.run(["open", write_report(results)])
    elif ans == "Change Gain…":
        return cmd_gain([r["path"] for r in ok]) if ok else 0
    return 0


# ---------------------------------------------------------------- gain

CHOICES = [
    ("+1 dB", ("db", 1.0)), ("+2 dB", ("db", 2.0)), ("+3 dB", ("db", 3.0)), ("+6 dB", ("db", 6.0)),
    ("−1 dB", ("db", -1.0)), ("−3 dB", ("db", -3.0)), ("−6 dB", ("db", -6.0)),
    ("Custom…", ("ask", None)),
    ("Match −14 LUFS (streaming)", ("lufs", -14.0)),
    ("Match −16 LUFS", ("lufs", -16.0)),
    ("Match −23 LUFS (broadcast)", ("lufs", -23.0)),
    ("Set true peak to −1 dBTP", ("tp", -1.0)),
]
LOSSY = {"mp3", "aac", "vorbis", "opus"}


def ask_choice(paths, infos):
    names = ", ".join(os.path.basename(p) for p in paths[:3]) + (" …" if len(paths) > 3 else "")
    prompt = "%s\nThe original stays untouched; a new file is written next to it." % names
    if any(i.get("codec") in LOSSY for i in infos):
        prompt += "\nNote: lossy files are re-encoded, which costs a little quality."
    script = ('on run argv\nactivate\nset L to items 2 thru -1 of argv\n'
              'set r to choose from list L with title "Loudscope — Change Gain" with prompt (item 1 of argv) '
              'default items {"+1 dB"} OK button name "Export" cancel button name "Cancel"\n'
              'if r is false then return ""\nreturn item 1 of r\nend run')
    ans = osa(script, prompt, *[c[0] for c in CHOICES])
    if not ans:
        return None
    mode = dict(CHOICES)[ans]
    if mode[0] == "ask":
        v = osa('activate\ndisplay dialog "Gain in dB (e.g. 2.5 or -1.5)" default answer "+2.0" '
                'with title "Loudscope — Change Gain" buttons {"Cancel", "Export"} default button 2 '
                'cancel button 1\nreturn text returned of result')
        if v is None:
            return None
        v = v.replace("−", "-").replace("dB", "").strip()
        try:
            return ("db", float(v))
        except ValueError:
            alert("Not a number", v)
            return None
    return mode


def ask_limiter(name, tp_after):
    ans = osa('on run argv\nactivate\nset r to display dialog (item 1 of argv) with title "Loudscope — Change Gain" '
              'buttons {"Skip", "Export as is", "Apply limiter"} default button 3 with icon caution\n'
              'return button returned of r\nend run',
              "%s\nWith this gain the true peak would be %s dBTP (ceiling %s dBTP)."
              % (name, fmt_db(tp_after), fmt_db(TP_CEILING)))
    return {"Apply limiter": "limit", "Export as is": "raw"}.get(ans, "skip")


def out_spec(path, info):
    """Output extension, encoder args and a codec tag, matched to the input."""
    ext = os.path.splitext(path)[1].lower()
    c = info["codec"]
    if ext in (".wav", ".wave", ".bwf") or (c.startswith("pcm_") and ext not in (".aif", ".aiff", ".aifc")):
        codec = c if c.startswith("pcm_") and c.endswith("le") else "pcm_s24le"
        return ".wav", ["-c:a", codec], codec
    if ext in (".aif", ".aiff", ".aifc"):
        codec = c if c.startswith("pcm_") and c.endswith("be") else "pcm_s24be"
        return ext, ["-c:a", codec], codec
    if c == "flac":
        if info["bits"] > 16:
            return ".flac", ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"], "flac24"
        return ".flac", ["-c:a", "flac", "-sample_fmt", "s16"], "pcm_s16"
    if c == "mp3":
        if not has_encoder("libmp3lame"):
            raise RuntimeError("This ffmpeg has no MP3 encoder (libmp3lame)")
        return ".mp3", ["-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3"], "mp3"
    if c == "aac":
        br = max(256, min(320, round((info["bit_rate"] or 256000) / 1000)))
        enc = "aac_at" if has_encoder("aac_at") else "aac"
        return (ext if ext in (".m4a", ".mp4", ".aac") else ".m4a"), ["-c:a", enc, "-b:a", "%dk" % br], "aac"
    if c == "alac":
        return ext, ["-c:a", "alac"], "alac"
    return ".wav", ["-c:a", "pcm_s24le"], "pcm_s24le"


def unique_path(p):
    if not os.path.exists(p):
        return p
    stem, ext = os.path.splitext(p)
    n = 2
    while os.path.exists("%s (%d)%s" % (stem, n, ext)):
        n += 1
    return "%s (%d)%s" % (stem, n, ext)


def render_gain(path, info, gain_db, limit, suffix):
    ext, enc, codec = out_spec(path, info)
    out = unique_path("%s_%s%s" % (os.path.splitext(path)[0], suffix, ext))
    filters = ["volume=%.3fdB" % gain_db]
    if limit:
        # 4x oversampled sample-peak limiter as a practical true-peak limiter
        sr = info["sr"] or 48000
        oversample = sr <= 96000
        if oversample:
            filters.append("aresample=%d" % (sr * 4))
        filters.append("alimiter=limit=%.5f:attack=3:release=60:level=0" % db_to_lin(TP_CEILING - LIMIT_MARGIN))
        if oversample:
            filters.append("aresample=%d" % sr)
    if "s16" in codec:
        filters.append("aresample=osf=s16:dither_method=triangular_hp")
    base = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", path, "-map_metadata", "0", "-map", "0:a:0"]
    tries = []
    if ext in (".mp3", ".m4a", ".mp4", ".flac"):
        tries.append(["-map", "0:v?", "-c:v", "copy"])  # keep cover art when possible
    tries.append([])
    err = ""
    for extra in tries:
        p = subprocess.run(base + extra + ["-af", ",".join(filters)] + enc + [out],
                           capture_output=True, text=True)
        if p.returncode == 0:
            return out
        err = p.stderr.strip()
        if os.path.exists(out):
            os.remove(out)
    raise RuntimeError("Export failed: " + err[-400:])


def cmd_gain(paths, mode=None, limit_policy=None):
    infos = []
    for p in paths:
        try:
            infos.append(probe(p))
        except Exception as e:
            infos.append({"error": str(e)})
    if mode is None:
        if QUIET:
            print("gain needs --db, --lufs or --tp with --quiet", file=sys.stderr)
            return 2
        mode = ask_choice(paths, infos)
        if mode is None:
            return 0
    kind, val = mode
    done, failed = [], 0
    for p, info in zip(paths, infos):
        name = os.path.basename(p)
        if "error" in info:
            alert(name, info["error"])
            failed += 1
            continue
        try:
            loud = loudness(p, frames=False)
            I, TP = loud.get("I"), loud.get("TP")
            if kind == "db":
                g, suffix = val, "%+.1fdB" % val
            elif kind == "lufs":
                if I is None or I <= -70:
                    raise RuntimeError("Too quiet to measure integrated loudness")
                g, suffix = val - I, "%gLUFS" % val
            else:
                if TP is None or TP == float("-inf"):
                    raise RuntimeError("Silent file, no peak to normalize")
                g, suffix = val - TP, "%gdBTP" % val
            tp_after = None if TP is None else TP + g
            limit = False
            if kind != "tp" and tp_after is not None and tp_after > TP_CEILING:
                policy = limit_policy or ("raw" if QUIET else ask_limiter(name, tp_after))
                if policy == "skip":
                    continue
                limit = policy == "limit"
            out = render_gain(p, info, g, limit, suffix)
            after = loudness(out, frames=False)
            # the limiter pulls loudness down a little; correct once
            if kind == "lufs" and limit and after.get("I") is not None and abs(val - after["I"]) > 0.15:
                os.remove(out)
                g += val - after["I"]
                out = render_gain(p, info, g, limit, suffix)
                after = loudness(out, frames=False)
            msg = "%s dB%s → %s LUFS, TP %s dBTP" % (
                fmt_db(g, 1, True), " + limiter" if limit else "",
                fmt_db(after.get("I")), fmt_db(after.get("TP")))
            done.append(out)
            if not QUIET:
                write_finder_comments([(out, after)])
            log("gain %s -> %s: %s" % (p, out, msg))
            print("%s | %s" % (out, msg))
            notify(os.path.basename(out), msg)
        except Exception as e:
            log("gain %s: %r" % (p, e))
            alert(name, str(e))
            failed += 1
    if done and not QUIET:
        subprocess.run(["open", "-R", done[-1]])
    return 1 if failed else 0


# ---------------------------------------------------------------- install / doctor

APPLESCRIPT = '''on run {input, parameters}
	set args to ""
	repeat with f in input
		set args to args & " " & quoted form of POSIX path of f
	end repeat
	if args is "" then return input
	do shell script %s & " " & %s & " %s" & args
	return input
end run'''


def as_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def workflow_info(menu):
    return {"NSServices": [{
        "NSBackgroundColorName": "background",
        "NSIconName": "NSActionTemplate",
        "NSMenuItem": {"default": menu},
        "NSMessage": "runWorkflowAsService",
        "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
        "NSSendFileTypes": ["public.audio"],
    }]}


def workflow_document(source):
    u = lambda: str(uuid.uuid4()).upper()
    finder = "/System/Library/CoreServices/Finder.app"
    action = {
        "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.applescript.object"]},
        "AMActionVersion": "1.0.2",
        "AMApplication": ["Automator"],
        "AMParameterProperties": {"source": {}},
        "AMProvides": {"Container": "List", "Types": ["com.apple.applescript.object"]},
        "ActionBundlePath": "/System/Library/Automator/Run AppleScript.action",
        "ActionName": "Run AppleScript",
        "ActionParameters": {"source": source},
        "BundleIdentifier": "com.apple.Automator.RunScript",
        "CFBundleVersion": "1.0.2",
        "CanShowSelectedItemsWhenRun": False,
        "CanShowWhenRun": True,
        "Category": ["AMCategoryUtilities"],
        "Class Name": "RunScriptAction",
        "InputUUID": u(), "OutputUUID": u(), "UUID": u(),
        "UnlocalizedApplications": ["Automator"],
        "arguments": {"0": {"default value": "", "name": "source", "required": "0", "type": "0", "uuid": "0"}},
        "conversionLabel": 0,
        "isViewVisible": 1,
        "location": "309.000000:368.000000",
        "nibPath": "/System/Library/Automator/Run AppleScript.action/Contents/Resources/Base.lproj/main.nib",
    }
    return {
        "AMApplicationBuild": "534", "AMApplicationVersion": "2.10", "AMDocumentVersion": "2",
        "actions": [{"action": action, "isViewVisible": 1}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleID": "com.apple.finder",
            "applicationBundleIDsByPath": {finder: "com.apple.finder"},
            "applicationPath": finder, "applicationPaths": [finder],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 15, "processesInput": False,
            "serviceApplicationBundleID": "com.apple.finder", "serviceApplicationPath": finder,
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }


def set_service_status(enabled):
    """Show (or forget) the Quick Actions in Finder's context menu.

    Services dropped into ~/Library/Services are registered but stay out of the
    context menu until they have an entry in the pbs NSServicesStatus preference.
    """
    try:
        raw = subprocess.run(["defaults", "export", "pbs", "-"], capture_output=True, check=True).stdout
        prefs = plistlib.loads(raw)
    except (subprocess.CalledProcessError, ValueError):
        prefs = {}
    status = prefs.setdefault("NSServicesStatus", {})
    for menu, _ in QUICK_ACTIONS:
        key = "(null) - %s - runWorkflowAsService" % menu
        if enabled:
            status[key] = {"presentation_modes": {
                "ContextMenu": True, "FinderPreview": True, "ServicesMenu": True, "TouchBar": False}}
        else:
            status.pop(key, None)
    subprocess.run(["defaults", "import", "pbs", "-"], input=plistlib.dumps(prefs), check=True)
    subprocess.run(["/System/Library/CoreServices/pbs", "-flush"], capture_output=True)


def find_ffmpeg():
    for d in os.environ["PATH"].split(":"):
        if d and os.access(os.path.join(d, "ffmpeg"), os.X_OK) and os.access(os.path.join(d, "ffprobe"), os.X_OK):
            return d
    return None


def cmd_install():
    if sys.platform != "darwin":
        print("Loudscope only runs on macOS.")
        return 1
    if sys.version_info < (3, 9):
        print("Python 3.9+ is required (found %s)." % sys.version.split()[0])
        return 1
    ffdir = find_ffmpeg()
    if not ffdir:
        print("ffmpeg/ffprobe not found. Install it first:  brew install ffmpeg")
        return 1
    python = os.path.realpath(sys.executable)
    os.makedirs(APP_DIR, exist_ok=True)
    dst = os.path.join(APP_DIR, "loudscope.py")
    if os.path.realpath(__file__) != os.path.realpath(dst):
        shutil.copy2(os.path.abspath(__file__), dst)
    os.chmod(dst, 0o755)
    cfg = load_config()
    with open(CONFIG_PATH, "w") as f:
        cfg.update({"version": VERSION, "python": python, "ffmpeg_dir": ffdir})
        cfg.setdefault("finder_comment", True)
        json.dump(cfg, f, indent=2)
    os.makedirs(SERVICES_DIR, exist_ok=True)
    for menu, cmd in QUICK_ACTIONS:
        wf = os.path.join(SERVICES_DIR, menu + ".workflow")
        if os.path.exists(wf):
            shutil.rmtree(wf)
        os.makedirs(os.path.join(wf, "Contents"))
        with open(os.path.join(wf, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump(workflow_info(menu), f)
        source = APPLESCRIPT % (as_str("'%s'" % python.replace("'", "'\\''")),
                                as_str("'%s'" % dst.replace("'", "'\\''")), cmd)
        with open(os.path.join(wf, "Contents", "document.wflow"), "wb") as f:
            plistlib.dump(workflow_document(source), f)
        print("installed  " + wf)
    set_service_status(True)
    print("installed  " + dst)
    print("python     " + python)
    print("ffmpeg     " + os.path.join(ffdir, "ffmpeg"))
    print("\nDone. Right-click an audio file in Finder → Quick Actions → Loudscope: …")
    print("If they don't show up yet, restart Finder:  killall Finder")
    return 0


def cmd_uninstall():
    for menu, _ in QUICK_ACTIONS:
        wf = os.path.join(SERVICES_DIR, menu + ".workflow")
        if os.path.exists(wf):
            shutil.rmtree(wf)
            print("removed  " + wf)
    for d in (APP_DIR, CACHE_DIR):
        if os.path.exists(d):
            shutil.rmtree(d)
            print("removed  " + d)
    set_service_status(False)
    return 0


def cmd_doctor():
    ok = True

    def check(label, cond, hint=""):
        nonlocal ok
        ok = ok and cond
        print("[%s] %s%s" % ("ok" if cond else "!!", label, "" if cond else "  → " + hint))
        return cond

    check("macOS", sys.platform == "darwin", "Loudscope only runs on macOS")
    check("Python %s" % sys.version.split()[0], sys.version_info >= (3, 9), "use Python 3.9+")
    ffdir = find_ffmpeg()
    if check("ffmpeg / ffprobe", bool(ffdir), "brew install ffmpeg"):
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        check("ffmpeg ebur128 filter", " ebur128 " in filters, "reinstall ffmpeg with default options")
        check("ffmpeg alimiter filter", " alimiter " in filters, "reinstall ffmpeg with default options")
        check("MP3 encoder (libmp3lame)", has_encoder("libmp3lame"), "only needed for MP3 export")
    cfg = load_config()
    check("installed script", os.path.exists(os.path.join(APP_DIR, "loudscope.py")), "run: loudscope.py install")
    if cfg.get("python"):
        check("configured python %s" % cfg["python"], os.access(cfg["python"], os.X_OK),
              "python moved; re-run install")
    for menu, _ in QUICK_ACTIONS:
        check("Quick Action “%s”" % menu, os.path.exists(os.path.join(SERVICES_DIR, menu + ".workflow")),
              "run: loudscope.py install")
    status = subprocess.run(["defaults", "read", "pbs", "NSServicesStatus"], capture_output=True, text=True).stdout
    check("Quick Actions enabled in context menu", status.count("(null) - Loudscope: ") >= len(QUICK_ACTIONS),
          "run: loudscope.py install")
    if ffdir:
        tmp = tempfile.mkdtemp(prefix="loudscope-doctor-")
        try:
            src = os.path.join(tmp, "tone.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=1000:d=3", "-af", "volume=-12dB",
                            "-c:a", "pcm_s24le", src], check=True)
            loud = loudness(src)
            check("analysis (1 kHz tone, I=%s LUFS)" % fmt_db(loud.get("I")), loud.get("I") is not None,
                  "see " + LOG_PATH)
            out = render_gain(src, probe(src), 3.0, True, "+3.0dB")
            check("gain export with limiter", os.path.exists(out), "see " + LOG_PATH)
        except Exception as e:
            check("self-test", False, str(e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("\nAll good." if ok else "\nSome checks failed.")
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def main(argv):
    global QUIET
    cmds = ("install", "uninstall", "doctor", "analyze", "gain")
    if len(argv) < 2 or argv[1] not in cmds:
        print(__doc__.strip())
        return 0 if len(argv) >= 2 and argv[1] in ("-h", "--help") else 2
    cmd, rest = argv[1], argv[2:]
    if cmd == "install":
        return cmd_install()
    if cmd == "uninstall":
        return cmd_uninstall()
    if cmd == "doctor":
        return cmd_doctor()
    mode, limit_policy, paths, view = None, None, [], "dialog"
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--db", "--lufs", "--tp") and i + 1 < len(rest):
            mode = (a[2:], float(rest[i + 1].replace("−", "-")))
            i += 2
            continue
        if a == "--limit":
            limit_policy = "limit"
        elif a == "--no-limit":
            limit_policy = "raw"
        elif a in ("--html", "--text"):
            view = a[2:]
        elif a == "--quiet":
            QUIET = True
        else:
            paths.append(a)
        i += 1
    missing = [p for p in paths if not os.path.isfile(p)]
    for p in missing:
        print("not a file: " + p, file=sys.stderr)
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        return 1
    return cmd_analyze(paths, view) if cmd == "analyze" else cmd_gain(paths, mode, limit_policy)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:
        log("fatal: %r" % e)
        alert("Loudscope error", str(e))
        sys.exit(1)
