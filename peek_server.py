#!/usr/bin/env python3
"""Dusk View: browse a folder and zoom into huge images (whole-slide pathology images included)
like in QuPath. Tiles are cut here, on the machine that has the files; a browser only shows them.

    python3 dusk_view.py --path /data/slides --port 8765

Started for you by the `peek` command (https://github.com/liuxiaZzz/peek). Needs Pillow; openslide-python + openslide-bin for
whole-slide formats (.svs .ndpi .mrxs .scn ...). Listens on 127.0.0.1 only.
"""
import argparse
import html
import io
import json
import math
import os
import re
import sys
import threading
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None          # large images are the point
except ImportError:
    Image = None
try:
    import openslide
    from openslide.deepzoom import DeepZoomGenerator
except Exception:                          # missing module or missing C library
    openslide = None
try:                                       # pyramidal TIFF / OME-TIFF (pyramid in SubIFDs, which
    import numpy                           # OpenSlide does not read)
    import tifffile
    import zarr
    # Import lazily-loaded helpers now, in the main thread. Otherwise the first request thread
    # imports them, and some (fsspec → an unrelated "snappy" package) install signal handlers,
    # which Python only allows in the main thread.
    for _m in ("imagecodecs", "numcodecs", "fsspec", "zarr.storage"):
        try:
            __import__(_m)
        except Exception:
            pass
except Exception:
    tifffile = zarr = None

WSI_EXT = {".svs", ".ndpi", ".mrxs", ".scn", ".vms", ".vmu", ".bif", ".svslide", ".tif", ".tiff", ".dcm", ".btf", ".tf2", ".tf8"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
TILE, OVERLAP, QUALITY = 254, 2, 82
BRAND = "Peek"


# ─── image sources ───────────────────────────────────────────────────────────────
class PilSource:
    """Deep-zoom pyramid for an ordinary image, built lazily level by level."""

    def __init__(self, path):
        img = Image.open(path)
        if img.size[0] * img.size[1] > 500_000_000:          # would need GBs of RAM and minutes
            raise RuntimeError(f"{img.size[0]}×{img.size[1]} is too large for a non-pyramidal image to browse smoothly")
        img.load()
        self.full = img.convert("RGB")
        self.width, self.height = self.full.size
        self.max_level = max(0, math.ceil(math.log2(max(self.width, self.height))))
        self.levels = {self.max_level: self.full}
        self.lock = threading.Lock()
        self.props = {"format": img.format or "image"}

    def level_size(self, level):
        s = 2 ** (self.max_level - level)
        return max(1, math.ceil(self.width / s)), max(1, math.ceil(self.height / s))

    def level_image(self, level):
        with self.lock:
            if level in self.levels:
                return self.levels[level]
            src = min(l for l in self.levels if l > level)
            img = self.levels[src]
            for l in range(src - 1, level - 1, -1):          # halve step by step (better quality)
                img = img.resize(self.level_size(l), Image.BILINEAR)
                if l >= self.max_level - 4:                 # keep the big ones, they are costly
                    self.levels[l] = img
            self.levels[level] = img
            return img

    def tile(self, level, col, row):
        w, h = self.level_size(level)
        x0, y0 = col * TILE - (OVERLAP if col else 0), row * TILE - (OVERLAP if row else 0)
        x1, y1 = min((col + 1) * TILE + OVERLAP, w), min((row + 1) * TILE + OVERLAP, h)
        if x0 >= w or y0 >= h:
            return None
        return self.level_image(level).crop((x0, y0, x1, y1))

    def thumbnail(self, size):
        t = self.full.copy()
        t.thumbnail((size, size))
        return t


class SlideSource:
    """Whole-slide image through OpenSlide's deep-zoom generator."""

    def __init__(self, path):
        self.slide = openslide.OpenSlide(path)
        w, h = self.slide.dimensions
        if self.slide.level_count == 1 and max(w, h) > 20000:
            raise RuntimeError(f"{w}×{h} but OpenSlide sees a single level (no pyramid); zooming would be very slow")
        self.dz = DeepZoomGenerator(self.slide, tile_size=TILE, overlap=OVERLAP, limit_bounds=True)
        self.width, self.height = self.dz.level_dimensions[-1]
        self.max_level = self.dz.level_count - 1
        p = self.slide.properties
        self.props = {k: p.get(v) for k, v in {
            "vendor": openslide.PROPERTY_NAME_VENDOR, "objective": openslide.PROPERTY_NAME_OBJECTIVE_POWER,
            "mpp_x": openslide.PROPERTY_NAME_MPP_X, "mpp_y": openslide.PROPERTY_NAME_MPP_Y}.items() if p.get(v)}

    def tile(self, level, col, row):
        cols, rows = self.dz.level_tiles[level]
        if col >= cols or row >= rows:
            return None
        return self.dz.get_tile(level, (col, row))

    def thumbnail(self, size):
        return self.slide.get_thumbnail((size, size)).convert("RGB")


# Older tifffile/zarr combinations hang when several threads open or read TIFFs at once, so all
# TIFF work goes through one lock. Each read is a few ms; opening a file takes ~1 s once.
TIFF_LOCK = threading.RLock()


class TiffPyramidSource:
    """Pyramidal TIFF / OME-TIFF read with tifffile + zarr: each deep-zoom tile is cut from the
    pyramid level closest to the requested scale, so only a few compressed tiles are decoded."""

    def __init__(self, path):
        with TIFF_LOCK:
            self._init(path)

    def _init(self, path):
        self.lock = TIFF_LOCK
        self.tif = tifffile.TiffFile(path)
        series = self.tif.series[0]
        axes = series.axes
        if "Y" not in axes or "X" not in axes:
            raise RuntimeError(f"unsupported axes {axes}")
        self.axes = axes
        store = series.aszarr()
        z = zarr.open(store, mode="r")
        self.levels = [z[str(i)] for i in range(len(series.levels))] if len(series.levels) > 1 else [z]
        shape0 = series.levels[0].shape
        self.height, self.width = shape0[axes.index("Y")], shape0[axes.index("X")]
        self.downs = [self.width / lv.shape[axes.index("X")] for lv in series.levels]
        self.max_level = max(0, math.ceil(math.log2(max(self.width, self.height))))
        self.props = {"format": "OME-TIFF" if self.tif.is_ome else "TIFF",
                      "levels": len(series.levels)}
        if self.tif.is_ome and self.tif.ome_metadata:
            om = self.tif.ome_metadata
            m = re.search(r'PhysicalSizeX="([^"]+)"', om)
            unit = re.search(r'PhysicalSizeXUnit="([^"]+)"', om)
            if m:
                v = float(m.group(1)); u = unit.group(1) if unit else "µm"
                v = v * {"nm": 1e-3, "mm": 1e3, "m": 1e6}.get(u, 1)
                self.props["mpp_x"] = f"{v:.6g}"
            mag = re.search(r'NominalMagnification="([^"]+)"', om)
            if mag:
                self.props["objective"] = mag.group(1)

    def _read(self, k, x0, y0, x1, y1):
        """Region [x0,x1)×[y0,y1) of pyramid level k as an RGB PIL image."""
        with self.lock:
            return self._read_unlocked(k, x0, y0, x1, y1)

    def _read_unlocked(self, k, x0, y0, x1, y1):
        idx = []
        for ax in self.axes:
            if ax == "Y": idx.append(slice(y0, y1))
            elif ax == "X": idx.append(slice(x0, x1))
            elif ax in "SC": idx.append(slice(None))
            else: idx.append(0)                      # T, Z, ...: first plane
        a = self.levels[k][tuple(idx)]
        keep = [ax for ax in self.axes if ax in "YXSC"]
        np = numpy
        a = np.asarray(a)
        order = [keep.index("Y"), keep.index("X")] + [i for i, ax in enumerate(keep) if ax in "SC"]
        a = a.transpose(order)
        if a.dtype != np.uint8:                      # e.g. 16-bit: stretch to 8-bit
            hi = float(a.max()) or 1.0
            a = (a.astype(np.float32) * (255.0 / hi)).astype(np.uint8)
        if a.ndim == 2:
            return Image.fromarray(a).convert("RGB")
        ch = a.shape[2]
        if ch == 1:
            return Image.fromarray(a[:, :, 0]).convert("RGB")
        if ch >= 3:
            return Image.fromarray(np.ascontiguousarray(a[:, :, :3]))
        return Image.fromarray(np.ascontiguousarray(a[:, :, 0])).convert("RGB")

    def level_size(self, level):
        s = 2 ** (self.max_level - level)
        return max(1, math.ceil(self.width / s)), max(1, math.ceil(self.height / s))

    def tile(self, level, col, row):
        w, h = self.level_size(level)
        x0, y0 = col * TILE - (OVERLAP if col else 0), row * TILE - (OVERLAP if row else 0)
        x1, y1 = min((col + 1) * TILE + OVERLAP, w), min((row + 1) * TILE + OVERLAP, h)
        if x0 >= w or y0 >= h:
            return None
        ds = 2 ** (self.max_level - level)                       # full-res pixels per tile pixel
        k = max(i for i, d in enumerate(self.downs) if d <= ds * 1.0001) if any(d <= ds * 1.0001 for d in self.downs) else 0
        d = self.downs[k]
        lw = self.levels[k].shape[self.axes.index("X")]
        lh = self.levels[k].shape[self.axes.index("Y")]
        fx0, fy0 = int(x0 * ds / d), int(y0 * ds / d)
        fx1, fy1 = min(lw, math.ceil(x1 * ds / d)), min(lh, math.ceil(y1 * ds / d))
        if fx1 <= fx0 or fy1 <= fy0:
            return None
        img = self._read(k, fx0, fy0, fx1, fy1)
        size = (x1 - x0, y1 - y0)
        return img if img.size == size else img.resize(size, Image.BILINEAR)

    def thumbnail(self, size):
        k = len(self.levels) - 1
        lw = self.levels[k].shape[self.axes.index("X")]; lh = self.levels[k].shape[self.axes.index("Y")]
        img = self._read(k, 0, 0, lw, lh)
        img.thumbnail((size, size))
        return img


_pyramid_cache, _pyramid_lock = {}, threading.Lock()


def is_pyramid_tiff(path):
    if not tifffile:
        return False
    with _pyramid_lock:
        if path in _pyramid_cache:
            return _pyramid_cache[path]
    try:
        with TIFF_LOCK, tifffile.TiffFile(path) as t:
            s = t.series[0]
            r = len(s.levels) > 1 or t.is_ome or t.pages[0].is_tiled
    except Exception:
        r = False
    with _pyramid_lock:
        _pyramid_cache[path] = r
    return r


class Sources:
    """Keeps a few recently used images open."""

    def __init__(self, keep=6):
        self.cache, self.keep, self.lock = OrderedDict(), keep, threading.Lock()
        self.opening = {}

    def get(self, path):
        with self.lock:
            if path in self.cache:
                self.cache.move_to_end(path)
                return self.cache[path]
            opening = self.opening.setdefault(path, threading.Lock())
        with opening:                              # first request opens it, others wait for that
            with self.lock:
                if path in self.cache:
                    return self.cache[path]
            src = open_source(path)
        with self.lock:
            self.opening.pop(path, None)
            self.cache[path] = src
            while len(self.cache) > self.keep:
                self.cache.popitem(last=False)
        return src


TIFF_EXT = {".tif", ".tiff", ".btf", ".tf2", ".tf8"}


def open_source(path):
    ext = os.path.splitext(path)[1].lower()
    errors = []
    if ext in TIFF_EXT and is_pyramid_tiff(path):
        try:
            return TiffPyramidSource(path)
        except Exception as e:
            errors.append(f"tifffile: {type(e).__name__}: {e}")
    if openslide and ext in WSI_EXT:
        try:
            return SlideSource(path)
        except Exception as e:                       # e.g. a plain (untiled) TIFF
            errors.append(f"openslide: {e}")
    if Image and ext in IMG_EXT:
        try:
            return PilSource(path)
        except Exception as e:
            errors.append(f"pillow: {e}")
    if ext in WSI_EXT - IMG_EXT and not openslide:
        raise RuntimeError("this slide format needs openslide: pip install --user openslide-python openslide-bin")
    raise RuntimeError("cannot open this file" + (f" ({'; '.join(errors)})" if errors else ""))


def kind(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in WSI_EXT - IMG_EXT or name.lower().endswith((".ome.tif", ".ome.tiff")):
        return "slide"
    return "image" if ext in IMG_EXT else None


# ─── web server ──────────────────────────────────────────────────────────────────
class App:
    def __init__(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            self.root, self.start = path, ""
        else:
            self.root, self.start = os.path.dirname(path), os.path.basename(path)
        self.sources = Sources()
        self.thumbs, self.thumb_lock = OrderedDict(), threading.Lock()
        self.host = os.uname().nodename.split(".")[0]

    def resolve(self, rel):
        full = os.path.realpath(os.path.join(self.root, rel or ""))
        root = os.path.realpath(self.root)
        if full != root and not full.startswith(root + os.sep):
            raise PermissionError("outside the folder being viewed")
        return full

    def listing(self, rel):
        full = self.resolve(rel)
        open_name = ""
        if os.path.isfile(full):                     # a link to an image: list its folder, open it
            full, open_name = os.path.dirname(full), os.path.basename(full)
        dirs, files = [], []
        with os.scandir(full) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        dirs.append({"name": e.name, "type": "dir"})
                    else:
                        k = kind(e.name)
                        if k:
                            files.append({"name": e.name, "type": k, "size": e.stat().st_size})
                except OSError:
                    pass
        key = lambda d: d["name"].lower()
        return {"path": os.path.relpath(full, self.root) if full != os.path.realpath(self.root) else "",
                "root": self.root, "host": self.host,
                "open": open_name, "entries": sorted(dirs, key=key) + sorted(files, key=key)}

    def thumb(self, rel, size=320):
        key = (rel, size)
        with self.thumb_lock:
            if key in self.thumbs:
                return self.thumbs[key]
        full = self.resolve(rel)
        ext = os.path.splitext(full)[1].lower()
        img = None
        if ext in TIFF_EXT and is_pyramid_tiff(full):
            try:
                img = self.sources.get(full).thumbnail(size)
            except Exception:
                img = None
        if img is None and openslide and ext in WSI_EXT:
            try:
                img = self.sources.get(full).thumbnail(size)     # SlideSource guards huge flat files
            except Exception:
                img = None
        if img is None:
            with Image.open(full) as im:
                im.draft("RGB", (size, size))               # fast path for JPEG
                img = im.convert("RGB")
                img.thumbnail((size, size))
        data = jpeg(img, 80)
        with self.thumb_lock:
            self.thumbs[key] = data
            while len(self.thumbs) > 400:
                self.thumbs.popitem(last=False)
        return data

    def info(self, rel):
        src = self.sources.get(self.resolve(rel))
        return {"width": src.width, "height": src.height, "tileSize": TILE, "overlap": OVERLAP,
                "maxLevel": src.max_level, "props": src.props}


def jpeg(img, q=QUALITY):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    return buf.getvalue()


IDLE_LIMIT = 3 * 3600          # exit after 3 h without requests, in case nobody stops us
last_request = [0.0]


def watchdog():
    import time
    last_request[0] = time.time()
    while True:
        time.sleep(60)
        if time.time() - last_request[0] > IDLE_LIMIT:
            os._exit(0)


def make_handler(app):
    import time

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype, cache=False):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            last_request[0] = time.time()
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    self.send(200, PAGE.replace("__START__", json.dumps(app.start))
                              .replace("__HOST__", html.escape(app.host))
                              .replace("__BRAND__", html.escape(BRAND)), "text/html; charset=utf-8")
                elif u.path == "/api/list":
                    self.send(200, json.dumps(app.listing(q.get("p", ""))), "application/json")
                elif u.path == "/api/info":
                    self.send(200, json.dumps(app.info(q["p"])), "application/json")
                elif u.path == "/api/thumb":
                    self.send(200, app.thumb(q["p"]), "image/jpeg", cache=True)
                elif u.path == "/api/tile":
                    src = app.sources.get(app.resolve(q["p"]))
                    t = src.tile(int(q["l"]), int(q["x"]), int(q["y"]))
                    if t is None:
                        self.send(404, "", "text/plain")
                    else:
                        self.send(200, jpeg(t), "image/jpeg", cache=True)
                else:
                    self.send(404, "not found", "text/plain")
            except PermissionError as e:
                self.send(403, str(e), "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self.send(500, json.dumps({"error": str(e)}), "application/json")
                except Exception:
                    pass
    return Handler


# ─── the page: folder browser + deep-zoom viewer (OpenSeadragon) ────────────────────
PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__BRAND__ · __HOST__</title>
<script src="https://cdn.jsdelivr.net/npm/openseadragon@4.1.1/build/openseadragon/openseadragon.min.js"></script>
<style>
:root{--bg:#0f1730;--panel:#16204099;--line:#ffffff1a;--fg:#f4f6ff;--mute:#8f9bc4;--pink:#ff9fd2;--blue:#86abff;--card:#1c2a52}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","JetBrains Mono",sans-serif}
header{position:fixed;inset:0 0 auto 0;height:52px;display:flex;align-items:center;gap:12px;padding:0 16px;
background:#0f1730cc;backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border-bottom:1px solid var(--line);z-index:10}
.pill{background:var(--pink);color:#2a1030;font-weight:700;border-radius:8px;padding:3px 10px;white-space:nowrap}
.host{background:#4a62ad;color:#fff;border-radius:8px;padding:3px 10px;white-space:nowrap}
.crumbs{display:flex;gap:4px;align-items:center;overflow:hidden;white-space:nowrap;min-width:0;flex:1}
.crumbs a{color:var(--blue);text-decoration:none;cursor:pointer}.crumbs a:hover{color:var(--pink)}.crumbs span{color:var(--mute)}
input{background:#ffffff12;border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:6px 10px;width:200px;outline:none}
input:focus{border-color:var(--pink)}
main{padding:68px 18px 24px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}
.card{display:block;color:inherit;text-decoration:none;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;transition:transform .12s,border-color .12s}
.card:hover{transform:translateY(-2px);border-color:var(--pink)}
.thumb{height:150px;display:flex;align-items:center;justify-content:center;background:#0b1226}
.thumb img{max-width:100%;max-height:100%;object-fit:contain}
.thumb .icon{font-size:46px;color:var(--blue)}
.meta{padding:8px 10px}.name{font-size:13px;word-break:break-all;line-height:1.35}.sub{font-size:12px;color:var(--mute)}
.empty{color:var(--mute);padding:40px;text-align:center}
#viewer{position:fixed;inset:52px 0 0 0;display:none;background:#0b1226}
#osd{width:100%;height:100%}
.hud{position:fixed;left:16px;bottom:16px;z-index:11;display:none;gap:8px;flex-wrap:wrap;align-items:center}
.chip{background:#0f1730cc;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--line);
border-radius:10px;padding:6px 10px;font-size:12px;color:var(--fg)}
.chip b{color:var(--pink)}
.btns{position:fixed;right:16px;top:64px;z-index:11;display:none;flex-direction:column;gap:6px}
.btns button{background:#0f1730cc;backdrop-filter:blur(14px);border:1px solid var(--line);color:var(--fg);border-radius:10px;
width:38px;height:38px;font-size:16px;cursor:pointer}.btns button:hover{border-color:var(--pink);color:var(--pink)}
#scalebar{height:6px;background:var(--pink);border-radius:3px;display:inline-block;vertical-align:middle;margin-right:6px}
.err{color:#ff8fb0}
</style></head><body>
<header><span class="pill">__BRAND__</span><span class="host">__HOST__</span><div class="crumbs" id="crumbs"></div>
<input id="filter" data-ph="filter" autocomplete="off"></header>
<main id="browser"><div class="grid" id="grid"></div></main>
<div id="viewer"><div id="osd"></div></div>
<div class="hud" id="hud"><span class="chip" id="title"></span><span class="chip" id="dims"></span>
<span class="chip" id="zoom"></span><span class="chip" id="scale"><span id="scalebar"></span><span id="scaletext"></span></span></div>
<div class="btns" id="btns"><button data-tip="back" onclick="back()">←</button>
<button data-tip="prev" onclick="step(-1)">‹</button><button data-tip="next" onclick="step(1)">›</button>
<button data-tip="fit" onclick="osd&&osd.viewport.goHome()">⤢</button>
<button data-tip="full" onclick="toggleFull()">⛶</button></div>
<script>
const START = __START__;
// UI language follows the browser: Chinese for zh-*, English otherwise
const ZH = (navigator.language || "").toLowerCase().startsWith("zh");
const T = ZH ? {root:"根目录", empty:"这里没有图片或子文件夹", folder:"文件夹", slide:"切片 · ", loading:"载入中…", levels:" 层",
  zoom:"缩放", newtab:"⌘+点击：在新标签页打开", filter:"筛选文件名…", back:"返回文件夹 (Esc)", prev:"上一张 (←)", next:"下一张 (→)",
  fit:"适应窗口 (0)", full:"全屏 (f)"}
 : {root:"Home", empty:"No images or folders here", folder:"Folder", slide:"Slide · ", loading:"Loading…", levels:" levels",
  zoom:"Zoom", newtab:"⌘-click: open in a new tab", filter:"Filter by name…", back:"Back to folder (Esc)", prev:"Previous (←)",
  next:"Next (→)", fit:"Fit to window (0)", full:"Full screen (f)"};
document.documentElement.lang = ZH ? "zh" : "en";
document.querySelectorAll("[data-tip]").forEach(b => b.title = T[b.dataset.tip]);
document.querySelectorAll("[data-ph]").forEach(i => i.placeholder = T[i.dataset.ph]);
let dir = "", entries = [], images = [], osd = null, current = null, mpp = null;
const $ = id => document.getElementById(id);
const enc = encodeURIComponent;
const fmt = n => n > 1e9 ? (n/1e9).toFixed(1)+" GB" : n > 1e6 ? (n/1e6).toFixed(1)+" MB" : (n/1e3).toFixed(0)+" KB";
const join = (a, b) => a ? a + "/" + b : b;

function crumbs(path, file) {
  const parts = path ? path.split("/") : [];
  let html = `<a href="#">${T.root}</a>`, acc = "";
  for (const p of parts) { acc = join(acc, p); html += `<span>/</span><a href="#${enc(acc)}">${esc(p)}</a>`; }
  if (file) html += `<span>/</span><span style="color:var(--fg)">${esc(file)}</span>`;
  $("crumbs").innerHTML = html;
}
function esc(s){return s.replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}

// The URL hash is the place (folder or image): links, ⌘-click into new tabs, back/forward and
// refresh all go through route().
let listed = null;
async function route() {
  const h = decodeURIComponent(location.hash.slice(1));
  const r = await fetch("/api/list?p=" + enc(h || START || "")); const d = await r.json();
  if (d.error) { showBrowser(); $("grid").innerHTML = `<div class="empty err">${esc(d.error)}</div>`; return; }
  if (listed !== d.path) { dir = d.path; entries = d.entries; images = entries.filter(e => e.type !== "dir"); listed = d.path; $("filter").value = ""; }
  if (d.open) { showImage(d.open); return; }
  showBrowser(); crumbs(dir); render();
}
function openDir(path) { location.hash = "#" + enc(path); }
function openImage(name) { location.hash = "#" + enc(join(dir, name)); }
window.addEventListener("hashchange", route);
function render() {
  const f = $("filter").value.toLowerCase();
  const list = entries.filter(e => !f || e.name.toLowerCase().includes(f));
  if (!list.length) { $("grid").innerHTML = `<div class="empty">${T.empty}</div>`; return; }
  $("grid").innerHTML = list.map(e => {
    const p = join(dir, e.name);
    if (e.type === "dir") return `<a class="card" href="#${enc(p)}"><div class="thumb"><span class="icon">📁</span></div>
      <div class="meta"><div class="name">${esc(e.name)}</div><div class="sub">${T.folder}</div></div></a>`;
    return `<a class="card" href="#${enc(p)}" title="${T.newtab}"><div class="thumb"><img loading="lazy" src="/api/thumb?p=${enc(p)}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'icon',textContent:'🖼'}))"></div>
      <div class="meta"><div class="name">${esc(e.name)}</div><div class="sub">${e.type === "slide" ? T.slide : ""}${fmt(e.size)}</div></div></a>`;
  }).join("");
}
$("filter").addEventListener("input", render);

function showBrowser() {
  $("browser").style.display = ""; $("viewer").style.display = "none";
  $("hud").style.display = "none"; $("btns").style.display = "none"; $("filter").style.display = ""; current = null;
}
async function showImage(name) {
  const p = join(dir, name); current = name;
  $("browser").style.display = "none"; $("viewer").style.display = "block"; $("filter").style.display = "none";
  $("hud").style.display = "flex"; $("btns").style.display = "flex"; crumbs(dir, name);
  $("title").innerHTML = `<b>${esc(name)}</b>`; $("dims").textContent = T.loading; $("zoom").textContent = ""; $("scale").style.display = "none";
  const r = await fetch("/api/info?p=" + enc(p)); const info = await r.json();
  if (info.error) { $("dims").innerHTML = `<span class="err">${esc(info.error)}</span>`; if (osd) osd.close(); return; }
  const pr = info.props || {};
  mpp = pr.mpp_x ? parseFloat(pr.mpp_x) : null;
  $("dims").textContent = `${info.width.toLocaleString()} × ${info.height.toLocaleString()} px` +
    (pr.objective ? ` · ${pr.objective}×` : "") + (mpp ? ` · ${mpp.toFixed(3)} µm/px` : "") + (pr.vendor ? ` · ${pr.vendor}` : "") +
    (pr.format && pr.format !== "PNG" && pr.format !== "JPEG" ? ` · ${pr.format}${pr.levels ? " " + pr.levels + T.levels : ""}` : "");
  const source = { width: info.width, height: info.height, tileSize: info.tileSize, tileOverlap: info.overlap,
    minLevel: 0, maxLevel: info.maxLevel, getTileUrl: (l, x, y) => `/api/tile?p=${enc(p)}&l=${l}&x=${x}&y=${y}` };
  if (!osd) {
    osd = OpenSeadragon({ element: $("osd"), showNavigationControl: false, showNavigator: true, navigatorPosition: "BOTTOM_RIGHT",
      navigatorBackground: "#0f1730", navigatorBorderColor: "#ff9fd2", navigatorDisplayRegionColor: "#ff9fd2",
      maxZoomPixelRatio: 4, visibilityRatio: 0.2, constrainDuringPan: false, animationTime: 0.6, springStiffness: 9,
      zoomPerScroll: 1.35, imageLoaderLimit: 8, timeout: 60000, preserveViewport: false, gestureSettingsMouse: { clickToZoom: false },
      subPixelRoundingForTransparency: OpenSeadragon.SUBPIXEL_ROUNDING_OCCURRENCES.ALWAYS });   // no hairline seams between tiles
    osd.addHandler("animation", hud); osd.addHandler("open", hud);
  }
  osd.open(source);
}
function hud() {
  if (!osd || !osd.world.getItemAt(0)) return;
  const item = osd.world.getItemAt(0);
  const z = item.viewportToImageZoom(osd.viewport.getZoom(true));       // screen px per image px
  $("zoom").textContent = `${T.zoom} ${(z * 100).toFixed(z < 0.1 ? 1 : 0)}%`;
  if (mpp) {
    const umPerScreenPx = mpp / z, target = 120 * umPerScreenPx;
    const nice = [1,2,5,10,20,50,100,200,500,1000,2000,5000,10000].find(v => v >= target * 0.5) || target;
    $("scalebar").style.width = (nice / umPerScreenPx) + "px";
    $("scaletext").textContent = nice >= 1000 ? (nice / 1000) + " mm" : nice + " µm"; $("scale").style.display = "";
  }
}
function back() { openDir(dir); }
function step(d) {
  if (!current || !images.length) return;
  let i = images.findIndex(e => e.name === current); i = (i + d + images.length) % images.length;
  openImage(images[i].name);
}
function toggleFull() { document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen(); }
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT") { if (e.key === "Escape") { e.target.value = ""; render(); e.target.blur(); } return; }
  if (current) {
    if (e.key === "Escape") back(); else if (e.key === "ArrowRight") step(1); else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "0") osd.viewport.goHome(); else if (e.key === "f") toggleFull();
  } else if (e.key === "/") { e.preventDefault(); $("filter").focus(); }
});
route();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Dusk View tile server")
    ap.add_argument("--path", default=".")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--brand", default="Peek", help="name shown in the page header")
    a = ap.parse_args()
    global BRAND
    BRAND = a.brand
    if Image is None:
        print("DUSK_VIEW_MISSING pillow", flush=True)
        sys.exit(3)
    path = os.path.abspath(os.path.expanduser(a.path))
    if not os.path.exists(path):
        print(f"DUSK_VIEW_ERROR no such file or folder: {path}", flush=True)
        sys.exit(2)
    app = App(path)
    try:
        class Server(ThreadingHTTPServer):
            request_queue_size = 128          # browsers fire many tile requests at once
            daemon_threads = True
        srv = Server(("127.0.0.1", a.port), make_handler(app))
    except OSError as e:
        print(f"DUSK_VIEW_ERROR port {a.port} is not available: {e}", flush=True)
        sys.exit(4)
    note = "" if (openslide or tifffile) else " (no openslide/tifffile: whole-slide formats need them)"
    threading.Thread(target=watchdog, daemon=True).start()
    print(f"DUSK_VIEW_READY {a.port}{note}", flush=True)
    if not openslide and not tifffile:
        print("DUSK_VIEW_NO_OPENSLIDE", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
