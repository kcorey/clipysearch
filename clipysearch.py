#!/usr/bin/python3
"""
ClipySearch - Browse and search Clipy clipboard history
Usage:  ./clipysearch.py [search_term]

No dependencies beyond Python 3 stdlib. Opens in your browser.
"""

import os, sys, json, plistlib, subprocess, threading, webbrowser, time, queue, signal, atexit
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, unquote

# ── Paths ─────────────────────────────────────────────────────────────────────
CLIPY_DATA_DIR = Path.home() / "Library" / "Application Support" / "Clipy"
PORT           = 57341      # fixed port so the URL never changes
MAX_ITEMS      = 3000
PID_FILE       = Path("/tmp/clipysearch.pid")

# ── NSKeyedArchiver decoder ───────────────────────────────────────────────────
def _resolve(obj, objects, depth=0):
    if depth > 20:
        return obj
    if isinstance(obj, plistlib.UID):
        inner = objects[obj.data]
        if inner == "$null":
            return None
        return _resolve(inner, objects, depth + 1)
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == "$class":
                cls = objects[v.data] if isinstance(v, plistlib.UID) else v
                result["_class"] = cls.get("$classname", "") if isinstance(cls, dict) else ""
            else:
                result[k] = _resolve(v, objects, depth + 1)
        return result
    if isinstance(obj, (list, tuple)):
        return [_resolve(v, objects, depth + 1) for v in obj]
    return obj


def parse_data_file(path):
    try:
        with open(path, "rb") as f:
            raw = plistlib.load(f)
        objects = raw.get("$objects", [])
        root = _resolve(raw["$top"]["root"], objects)
        if not isinstance(root, dict):
            return None

        string_val = root.get("stringValue") or ""
        image_data = root.get("image")
        url_val    = root.get("URL") or ""
        filenames  = root.get("filenames")

        # has_image: NSImage stored as dict with NSReps; also handle raw bytes
        has_image = (isinstance(image_data, dict) and "NSReps" in image_data) or \
                    (isinstance(image_data, (bytes, bytearray)) and len(image_data) > 0)

        # has_files: only if NSArray actually contains paths (empty NSArray → not files)
        files_list = []
        if isinstance(filenames, dict):
            ns_objs = filenames.get("NS.objects", [])
            files_list = [str(f) for f in ns_objs if f]
        elif isinstance(filenames, list):
            files_list = [str(f) for f in filenames if f]
        has_files = len(files_list) > 0

        has_url = bool(url_val and isinstance(url_val, str) and url_val.strip())

        # Also check pasteboard types as fallback for image detection
        types_obj = root.get("types") or {}
        type_list = types_obj.get("NS.objects", []) if isinstance(types_obj, dict) else []
        if not has_image and any(t in str(type_list) for t in ("PNG", "TIFF", "NSTIFFPboardType")):
            has_image = True

        if has_image:
            content_type = "image"
        elif has_files:
            content_type = "files"
        elif has_url and not string_val:
            content_type = "url"
        else:
            content_type = "text"

        # Build display text
        if content_type == "image":
            text = ""
        elif content_type == "files":
            text = "\n".join(files_list)
        else:
            text = (string_val if isinstance(string_val, str) else "") or \
                   (url_val   if isinstance(url_val,    str) else "")

        stat = path.stat()
        return {
            "uuid":  path.stem,
            "type":  content_type,
            "text":  text.strip(),
            "mtime": stat.st_mtime,
            "size":  stat.st_size,
        }
    except Exception:
        return None


def load_all():
    files = sorted(
        CLIPY_DATA_DIR.glob("*.data"),
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )[:MAX_ITEMS]
    items = []
    for path in files:
        item = parse_data_file(path)
        if item:
            items.append(item)
    return items


_img_cache   = {}   # uuid → (png_bytes, mime)
_thumb_cache = {}   # uuid → png_bytes (thumbnail)

# ── SSE / lifecycle state ─────────────────────────────────────────────────────
_sse_clients      = set()   # set of queue.Queue, one per connected tab
_sse_lock         = threading.Lock()
_server_ref       = None    # set in main() so SSE handler can shut it down
_last_client_seen = [time.time()]  # mutable so threads can update it

_ctrl_c_event   = threading.Event()   # set when Ctrl-C pressed
_relaunch_event = threading.Event()   # set when browser requests relaunch
_quit_requested = threading.Event()   # set when browser tab closes (beforeunload beacon)
_ctrl_c_count   = [0]


def write_pid():
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))


def kill_existing():
    """Kill any running clipysearch instance recorded in the PID file."""
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid == os.getpid():
            return  # shouldn't happen, but be safe
        os.kill(old_pid, signal.SIGKILL)
        print(f"Killed previous clipysearch (pid {old_pid})")
        # Wait up to 2 s for the port to be released
        for _ in range(20):
            if not port_in_use(PORT):
                break
            time.sleep(0.1)
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _extract_raw_image(uuid):
    """Return raw image bytes from a .data file (TIFF, PNG, JPEG, or GIF)."""
    path = CLIPY_DATA_DIR / f"{uuid}.data"
    if not path.exists():
        return None
    with open(path, "rb") as f:
        raw = plistlib.load(f)
    objects = raw.get("$objects", [])
    root = _resolve(raw["$top"]["root"], objects)
    img = root.get("image") if isinstance(root, dict) else None
    if img is None:
        return None
    # NSImage is a dict — dig into NSReps → NSTIFFRepresentation
    if isinstance(img, dict):
        reps_outer = img.get("NSReps", {})
        if isinstance(reps_outer, dict):
            for outer in (reps_outer.get("NS.objects") or []):
                if isinstance(outer, dict):
                    for inner in (outer.get("NS.objects") or []):
                        if isinstance(inner, dict):
                            tiff = inner.get("NSTIFFRepresentation")
                            if isinstance(tiff, (bytes, bytearray)) and len(tiff) > 0:
                                return bytes(tiff)
    if isinstance(img, (bytes, bytearray)) and len(img) > 0:
        return bytes(img)
    return None


def _sips_convert(raw_bytes, extra_args=()):
    """Convert raw image bytes to PNG via sips. extra_args e.g. ['-Z','80']."""
    import tempfile
    # Detect input format for file extension
    if raw_bytes[:3] == b"\xff\xd8\xff":
        in_suffix = ".jpg"
    elif raw_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        in_suffix = ".png"
    else:
        in_suffix = ".tiff"

    with tempfile.NamedTemporaryFile(suffix=in_suffix, delete=False) as tf:
        tf.write(raw_bytes)
        in_path = tf.name
    out_path = in_path.rsplit(".", 1)[0] + "_out.png"
    try:
        cmd = ["sips", "-s", "format", "png"] + list(extra_args) + [in_path, "--out", out_path]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and os.path.exists(out_path):
            with open(out_path, "rb") as pf:
                return pf.read()
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass
    return None


def get_image_bytes(uuid):
    """Return (png_bytes, mime) for a clipboard image."""
    if uuid in _img_cache:
        return _img_cache[uuid]
    try:
        raw = _extract_raw_image(uuid)
        if not raw:
            return None, None
        # Already PNG or JPEG — serve directly
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            result = (raw, "image/png");  _img_cache[uuid] = result;  return result
        if raw[:3] == b"\xff\xd8\xff":
            result = (raw, "image/jpeg"); _img_cache[uuid] = result;  return result
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            result = (raw, "image/gif");  _img_cache[uuid] = result;  return result
        # TIFF → convert to PNG
        png = _sips_convert(raw)
        if png:
            result = (png, "image/png");  _img_cache[uuid] = result;  return result
        # Fallback: serve raw TIFF (Safari can display it)
        result = (raw, "image/tiff");     _img_cache[uuid] = result;  return result
    except Exception:
        return None, None


def get_thumb_bytes(uuid):
    """Return small PNG thumbnail bytes (max 100px), cached."""
    if uuid in _thumb_cache:
        return _thumb_cache[uuid]
    try:
        raw = _extract_raw_image(uuid)
        if not raw:
            return None
        png = _sips_convert(raw, extra_args=["-Z", "100"])
        if png:
            _thumb_cache[uuid] = png
            return png
    except Exception:
        pass
    return None


def copy_image_to_clipboard(uuid):
    """Put clipboard image back onto the system clipboard via osascript."""
    img_bytes, mime = get_image_bytes(uuid)
    if not img_bytes:
        return False
    import tempfile
    # Write PNG (or TIFF) to temp file and use osascript to set clipboard
    suffix = ".png" if mime == "image/png" else ".tiff"
    nsa_type = "PNG picture" if mime == "image/png" else "TIFF picture"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(img_bytes)
        tmp_path = tf.name
    try:
        script = f'set the clipboard to (read (POSIX file "{tmp_path}") as {nsa_type})'
        subprocess.run(["osascript", "-e", script], check=False)
        return True
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def copy_to_clipboard(text):
    if sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
    elif sys.platform.startswith("linux"):
        try:
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode("utf-8"), check=False)
        except FileNotFoundError:
            pass
    else:
        subprocess.run(["clip"], input=text.encode("utf-16"), check=False)


# ── Embedded HTML/CSS/JS UI ───────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ClipySearch</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:       #1e1e2e;
    --bg2:      #181825;
    --bg3:      #313244;
    --bg-hover: #313244;
    --bg-sel:   #45475a;
    --border:   #45475a;
    --fg:       #cdd6f4;
    --fg-dim:   #6c7086;
    --accent:   #89b4fa;
    --green:    #a6e3a1;
    --red:      #f38ba8;
    --yellow:   #f9e2af;
    --orange:   #fab387;
    --font-ui:  -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    --font-mono: "Menlo", "Cascadia Code", "Fira Code", monospace;
  }

  html, body { height: 100%; overflow: hidden; background: var(--bg); color: var(--fg); font-family: var(--font-ui); }

  /* ── Layout ── */
  #app { display: flex; flex-direction: column; height: 100vh; }

  /* ── Top bar ── */
  #topbar {
    display: flex; align-items: center; gap: 12px;
    background: var(--bg2); padding: 10px 16px;
    border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  #logo { color: var(--accent); font-weight: 700; font-size: 15px; white-space: nowrap; }
  #search-wrap {
    flex: 1; display: flex; align-items: center; gap: 6px;
    background: var(--bg3); border-radius: 8px; padding: 6px 10px;
  }
  #search-wrap svg { flex-shrink: 0; opacity: .5; }
  #search {
    flex: 1; background: none; border: none; outline: none;
    color: var(--fg); font-size: 14px; font-family: var(--font-ui);
  }
  #search::placeholder { color: var(--fg-dim); }
  #stats { color: var(--fg-dim); font-size: 12px; white-space: nowrap; }
  #loader {
    color: var(--accent); font-size: 12px; white-space: nowrap;
    animation: pulse 1s ease-in-out infinite alternate;
  }
  @keyframes pulse { from { opacity: .4; } to { opacity: 1; } }

  /* ── Panes ── */
  #panes { display: flex; flex: 1; overflow: hidden; }

  /* ── List pane ── */
  #list-pane {
    width: 380px; flex-shrink: 0;
    display: flex; flex-direction: column;
    background: var(--bg2); border-right: 1px solid var(--border);
  }
  #list { flex: 1; overflow-y: auto; }
  #list::-webkit-scrollbar { width: 6px; }
  #list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .item {
    display: flex; align-items: flex-start; gap: 6px;
    padding: 8px 10px; border-bottom: 1px solid rgba(69,71,90,.4);
    cursor: pointer; transition: background .1s;
    position: relative;
  }
  .item:hover  { background: var(--bg-hover); }
  .item.active { background: var(--bg-sel); }

  .badge {
    flex-shrink: 0; width: 20px; height: 20px; border-radius: 4px;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; margin-top: 1px;
  }
  .badge-text  { background: rgba(137,180,250,.15); color: var(--accent); }
  .badge-image { background: rgba(166,227,161,.15); color: var(--green); }
  .badge-url   { background: rgba(243,139,168,.15); color: var(--red); }
  .badge-files { background: rgba(250,179,135,.15); color: var(--orange); }

  .item-body { flex: 1; min-width: 0; }
  .preview {
    font-family: var(--font-mono); font-size: 12px;
    color: var(--fg); line-height: 1.5;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden; word-break: break-all;
    white-space: pre-wrap;
  }
  .preview.empty { color: var(--fg-dim); font-style: italic; }
  .item-meta { font-size: 10px; color: var(--fg-dim); margin-top: 3px; }

  .item-thumb {
    flex-shrink: 0; width: 72px; height: 52px;
    object-fit: cover; border-radius: 4px;
    background: var(--bg3); align-self: center;
  }
  .item-thumb-placeholder {
    flex-shrink: 0; width: 72px; height: 52px; border-radius: 4px;
    background: var(--bg3); display: flex; align-items: center;
    justify-content: center; font-size: 20px; align-self: center;
  }

  .copy-btn {
    flex-shrink: 0; background: var(--bg3); border: none; cursor: pointer;
    color: var(--fg-dim); font-size: 13px; border-radius: 5px;
    padding: 3px 6px; transition: background .15s, color .15s;
    align-self: center;
  }
  .copy-btn:hover { background: var(--accent); color: var(--bg); }
  .copy-btn.flashed { background: var(--green) !important; color: var(--bg) !important; }

  /* ── Detail pane ── */
  #detail-pane { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

  #detail-header {
    display: flex; align-items: center; gap: 8px;
    background: var(--bg2); padding: 8px 14px;
    border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  #detail-meta { flex: 1; font-size: 11px; color: var(--fg-dim); }
  #copy-detail {
    background: var(--bg3); border: none; cursor: pointer;
    color: var(--fg); font-size: 13px; border-radius: 6px;
    padding: 5px 12px; transition: background .15s, color .15s;
  }
  #copy-detail:hover { background: var(--accent); color: var(--bg); }
  #copy-detail.flashed { background: var(--green) !important; color: var(--bg) !important; }

  #detail-body { flex: 1; overflow-y: auto; padding: 16px; }
  #detail-body::-webkit-scrollbar { width: 6px; }
  #detail-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  #detail-text {
    font-family: var(--font-mono); font-size: 13px; line-height: 1.65;
    color: var(--fg); white-space: pre-wrap; word-break: break-word;
  }
  #detail-text .hl { background: var(--yellow); color: #1e1e2e; border-radius: 2px; }
  #empty-state { color: var(--fg-dim); font-size: 14px; text-align: center; margin-top: 80px; }

  /* ── Image detail ── */
  #detail-image-wrap { display: none; text-align: center; padding: 8px 0 16px; }
  #detail-image-wrap img {
    max-width: 100%; max-height: 70vh;
    border-radius: 8px; box-shadow: 0 4px 24px rgba(0,0,0,.5);
  }
  #detail-image-info { color: var(--fg-dim); font-size: 11px; margin-top: 8px; }

  /* ── No results ── */
  #no-results { display: none; color: var(--fg-dim); font-size: 13px; text-align: center; padding: 40px 16px; }
</style>
</head>
<body>
<div id="app">

  <!-- Top bar -->
  <div id="topbar">
    <div id="logo">⌨ ClipySearch</div>
    <div id="search-wrap">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <input id="search" type="text" placeholder="Search clipboard history…" autocomplete="off" autofocus>
    </div>
    <span id="loader">Loading…</span>
    <span id="stats" style="display:none"></span>
  </div>

  <!-- Panes -->
  <div id="panes">

    <!-- List -->
    <div id="list-pane">
      <div id="list"></div>
      <div id="no-results">No matches</div>
    </div>

    <!-- Detail -->
    <div id="detail-pane">
      <div id="detail-header">
        <span id="detail-meta">Hover or select an item</span>
        <button id="copy-detail">⎘ Copy</button>
      </div>
      <div id="detail-body">
        <div id="empty-state">← Select an item to see its full content</div>
        <pre id="detail-text" style="display:none"></pre>
        <div id="detail-image-wrap">
          <img id="detail-image" src="" alt="clipboard image">
          <div id="detail-image-info"></div>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
const $ = id => document.getElementById(id);

let ALL_ITEMS = [];
let filtered  = [];
let activeIdx = -1;

// ── Load data ────────────────────────────────────────────────────────────────
fetch('/api/items')
  .then(r => r.json())
  .then(data => {
    ALL_ITEMS = data;
    $('loader').style.display = 'none';
    $('stats').style.display  = '';
    applyFilter($('search').value);

    // Pre-fill search from URL hash
    const q = decodeURIComponent(location.hash.slice(1));
    if (q) { $('search').value = q; applyFilter(q); }
  });

// ── Search ───────────────────────────────────────────────────────────────────
$('search').addEventListener('input', e => {
  applyFilter(e.target.value);
  location.hash = encodeURIComponent(e.target.value);
});

function applyFilter(q) {
  const lo = q.trim().toLowerCase();
  filtered = lo ? ALL_ITEMS.filter(i => i.text.toLowerCase().includes(lo)) : ALL_ITEMS.slice();
  $('stats').textContent = lo
    ? `${filtered.length} / ${ALL_ITEMS.length} items`
    : `${ALL_ITEMS.length} items`;
  renderList(lo);
  activeIdx = -1;
  clearDetail();
}

// ── Render list ──────────────────────────────────────────────────────────────
const BADGES = { text:'T', image:'🖼', url:'🔗', files:'📁' };

function makeRow(idx, item) {
  const div = document.createElement('div');
  div.className = 'item';
  div.dataset.idx = idx;

  const badge = document.createElement('div');
  badge.className = `badge badge-${item.type}`;
  badge.textContent = BADGES[item.type] || 'T';

  if (item.type === 'image') {
    const ph = document.createElement('div');
    ph.className = 'item-thumb-placeholder';
    ph.textContent = '🖼';
    const thumb = document.createElement('img');
    thumb.className = 'item-thumb';
    thumb.alt = 'image clip';
    thumb.style.display = 'none';
    div.append(thumb, ph);
    thumb.onload  = () => { thumb.style.display = 'block'; ph.style.display = 'none'; };
    thumb.onerror = () => {};
    thumb.src = `/api/thumb/${item.uuid}`;
  }

  const body = document.createElement('div');
  body.className = 'item-body';

  const preview = document.createElement('div');
  preview.className = 'preview' + (!item.text && item.type !== 'image' ? ' empty' : '');
  if (item.type === 'image') {
    preview.textContent = `Image  ·  ${(item.size/1024).toFixed(0)} KB`;
  } else {
    preview.textContent = item.text
      ? item.text.split('\n').slice(0,2).join('\n').slice(0,300)
      : '(empty)';
  }

  const meta = document.createElement('div');
  meta.className = 'item-meta';
  const d = new Date(item.mtime * 1000);
  meta.textContent = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});

  body.append(preview, meta);

  const btn = document.createElement('button');
  btn.className = 'copy-btn';
  btn.title = 'Copy to clipboard';
  btn.innerHTML = '⎘';
  // Read idx from dataset dynamically so prepend-shifts stay correct
  btn.addEventListener('click', e => { e.stopPropagation(); copyItem(parseInt(div.dataset.idx), btn); });

  div.append(badge, body, btn);

  div.addEventListener('mouseenter', () => showDetail(parseInt(div.dataset.idx)));
  div.addEventListener('click',      () => selectItem(parseInt(div.dataset.idx)));

  return div;
}

function renderList(query) {
  const list = $('list');
  list.innerHTML = '';
  $('no-results').style.display = filtered.length ? 'none' : 'block';
  filtered.forEach((item, i) => list.appendChild(makeRow(i, item)));
}

// ── Select / highlight ───────────────────────────────────────────────────────
function selectItem(i, query) {
  query = query ?? $('search').value.trim();
  document.querySelectorAll('.item').forEach(el => el.classList.remove('active'));
  const el = document.querySelector(`.item[data-idx="${i}"]`);
  if (el) el.classList.add('active');
  activeIdx = i;
  showDetail(i, query);
}

function showDetail(i, query) {
  query = query ?? $('search').value.trim();
  const item = filtered[i];
  if (!item) return;

  // Meta
  const d = new Date(item.mtime * 1000);
  const typeLabel = {text:'Text',image:'Image',url:'URL',files:'Files'}[item.type] || item.type;
  const charInfo = item.type === 'image' ? `${(item.size/1024).toFixed(1)} KB` : `${item.text.length} chars`;
  $('detail-meta').textContent = `${typeLabel}  ·  ${d.toLocaleString()}  ·  ${charInfo}`;

  $('empty-state').style.display = 'none';

  if (item.type === 'image') {
    // Show image
    $('detail-text').style.display = 'none';
    const wrap = $('detail-image-wrap');
    wrap.style.display = 'block';
    const img = $('detail-image');
    $('detail-image-info').textContent = 'Loading…';
    img.src = `/api/image/${item.uuid}?t=${item.mtime}`;
    img.onload = () => {
      $('detail-image-info').textContent = `${img.naturalWidth} × ${img.naturalHeight} px`;
    };
    img.onerror = () => {
      $('detail-image-info').textContent = '(image not available — try clicking the item)';
    };
  } else {
    // Show text
    $('detail-image-wrap').style.display = 'none';
    const dt = $('detail-text');
    dt.style.display = '';

    if (query) {
      const lo = query.toLowerCase();
      const text = item.text;
      let html = '';
      let pos = 0;
      while (pos < text.length) {
        const found = text.toLowerCase().indexOf(lo, pos);
        if (found === -1) { html += esc(text.slice(pos)); break; }
        html += esc(text.slice(pos, found));
        html += `<span class="hl">${esc(text.slice(found, found + query.length))}</span>`;
        pos = found + query.length;
      }
      dt.innerHTML = html || '(empty)';
    } else {
      dt.textContent = item.text || '(empty)';
    }
    dt.scrollTop = 0;
  }
}

function clearDetail() {
  $('detail-meta').textContent = 'Hover or select an item';
  $('empty-state').style.display = '';
  $('detail-text').style.display = 'none';
  $('detail-text').textContent = '';
  $('detail-image-wrap').style.display = 'none';
  $('detail-image').src = '';
}

function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Copy ─────────────────────────────────────────────────────────────────────
function copyItem(i, btn) {
  const item = filtered[i];
  if (!item) return;
  serverCopy(item.text, btn);
}

$('copy-detail').addEventListener('click', () => {
  if ($('detail-image-wrap').style.display !== 'none') {
    // Copy image back to clipboard
    const uuid = $('detail-image').src.split('/').pop();
    if (uuid) {
      fetch(`/api/copy-image/${uuid}`, {method:'POST'})
        .then(r => { if (r.ok) flash($('copy-detail')); });
    }
    return;
  }
  const dt = $('detail-text');
  if (dt.style.display === 'none') return;
  serverCopy(dt.textContent, $('copy-detail'));
});

function serverCopy(text, btn) {
  fetch('/api/copy', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text})
  }).then(r => {
    if (r.ok && btn) flash(btn);
  });
}

function flash(btn) {
  const orig = btn.innerHTML;
  btn.innerHTML = '✓ Copied';
  btn.classList.add('flashed');
  setTimeout(() => { btn.innerHTML = orig; btn.classList.remove('flashed'); }, 1200);
}

// ── Keyboard ─────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    $('search').value = ''; $('search').focus();
    applyFilter(''); return;
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    const n = Math.min(activeIdx + 1, filtered.length - 1);
    selectItem(n);
    scrollToActive();
    return;
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    const n = Math.max(activeIdx - 1, 0);
    selectItem(n);
    scrollToActive();
    return;
  }
  if (e.key === 'Enter' && activeIdx >= 0) {
    copyItem(activeIdx);
    return;
  }
  // Typing: focus search
  if (!e.ctrlKey && !e.metaKey && e.key.length === 1) {
    $('search').focus();
  }
});

function scrollToActive() {
  const el = document.querySelector('.item.active');
  if (el) el.scrollIntoView({block: 'nearest'});
}

// ── Quit on window close ─────────────────────────────────────────────────────
window.addEventListener('beforeunload', () => {
  navigator.sendBeacon('/api/quit', '');
});

// ── SSE lifecycle ─────────────────────────────────────────────────────────────
(function connectSSE() {
  const es = new EventSource('/api/events');
  es.addEventListener('newitem', e => {
    const item = JSON.parse(e.data);
    ALL_ITEMS.unshift(item);

    const query = $('search').value.trim().toLowerCase();
    const matches = !query || item.text.toLowerCase().includes(query);

    if (matches) {
      filtered.unshift(item);
      // Shift all existing data-idx values up by 1
      document.querySelectorAll('#list .item').forEach(el => {
        el.dataset.idx = parseInt(el.dataset.idx) + 1;
      });
      if (activeIdx >= 0) activeIdx++;

      // Prepend row while keeping the user's scroll position
      const list = $('list');
      const prevScroll = list.scrollTop;
      const row = makeRow(0, item);
      list.insertBefore(row, list.firstChild);
      // If user was scrolled down, offset by the new row's height so view doesn't jump
      if (prevScroll > 0) list.scrollTop = prevScroll + row.offsetHeight;
      $('no-results').style.display = 'none';
    }

    $('stats').textContent = query
      ? `${filtered.length} / ${ALL_ITEMS.length} items`
      : `${ALL_ITEMS.length} items`;
  });

  es.addEventListener('shutdown', () => {
    es.close();
    document.body.insertAdjacentHTML('beforeend', `
      <div id="shutdown-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.8);
           display:flex;align-items:center;justify-content:center;z-index:9999;
           font-family:-apple-system,sans-serif;color:#cdd6f4;">
        <div style="text-align:center;padding:40px 60px;background:#1e1e2e;
                    border-radius:16px;border:1px solid #45475a;min-width:320px;">
          <div style="font-size:32px;margin-bottom:12px">⌨</div>
          <div style="font-size:18px;font-weight:600;margin-bottom:6px">ClipySearch stopped</div>
          <div id="relaunch-status" style="font-size:13px;color:#6c7086;margin-bottom:20px">
            Click to relaunch, or run <code style="background:#313244;
            padding:2px 6px;border-radius:4px">./clipysearch.py</code> in your terminal
          </div>
          <button id="relaunch-btn" onclick="doRelaunch()"
            style="background:#89b4fa;color:#1e1e2e;border:none;border-radius:8px;
                   padding:10px 28px;font-size:14px;font-weight:600;cursor:pointer;">
            🔄 Relaunch
          </button>
        </div>
      </div>`);
  });

  window.doRelaunch = function() {
    const btn = document.getElementById('relaunch-btn');
    const status = document.getElementById('relaunch-status');
    btn.disabled = true;
    btn.textContent = 'Relaunching…';
    // Ask server to spawn a new process and release the port
    fetch('/api/relaunch', {method:'POST'})
      .then(() => {
        status.textContent = 'Waiting for new server…';
        pollForServer();
      })
      .catch(() => {
        // Server already gone — just poll (user may have manually restarted)
        status.textContent = 'Waiting for server…';
        pollForServer();
      });
  };

  function pollForServer() {
    fetch('/api/ping').then(r => {
      if (r.ok) {
        document.getElementById('relaunch-btn').textContent = '✓ Reconnecting…';
        setTimeout(() => location.reload(), 300);
      } else {
        setTimeout(pollForServer, 800);
      }
    }).catch(() => setTimeout(pollForServer, 800));
  }
  es.onerror = () => {
    // Connection dropped — try to reconnect after 3s (handles page refresh)
    es.close();
    setTimeout(connectSSE, 3000);
  };
})();
</script>
</body>
</html>"""


# ── HTTP request handler ───────────────────────────────────────────────────────
_items_cache = None
_cache_lock  = threading.Lock()


def get_items():
    global _items_cache
    with _cache_lock:
        if _items_cache is None:
            _items_cache = load_all()
        return _items_cache


def sse_broadcast(event, data=""):
    msg = (f"event: {event}\ndata: {data}\n\n").encode()
    with _sse_lock:
        for q in list(_sse_clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence access log

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/items":
            items = get_items()
            # Only send what the browser needs (skip raw bytes)
            payload = json.dumps(items, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(payload))
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/api/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if parsed.path == "/api/events":
            # Server-Sent Events — one long-lived connection per tab
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = queue.Queue(maxsize=10)
            with _sse_lock:
                _sse_clients.add(q)
                _last_client_seen[0] = time.time()
                _quit_requested.clear()   # new tab connected — cancel any pending quit
            try:
                # Send initial "connected" event
                self.wfile.write(b"event: connected\ndata: ok\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=20)
                        self.wfile.write(msg)
                        self.wfile.flush()
                    except queue.Empty:
                        # Keepalive ping
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _sse_lock:
                    _sse_clients.discard(q)
                # If no clients remain, start countdown to auto-quit
                def _maybe_shutdown():
                    # Short wait if browser explicitly signalled close; else 15 s
                    wait = 1 if _quit_requested.is_set() else 15
                    time.sleep(wait)
                    with _sse_lock:
                        if not _sse_clients and _server_ref:
                            print("\nAll browser tabs closed — shutting down.")
                            threading.Thread(
                                target=_server_ref.shutdown, daemon=True
                            ).start()
                threading.Thread(target=_maybe_shutdown, daemon=True).start()
            return

        if parsed.path.startswith("/api/thumb/"):
            uuid = parsed.path[len("/api/thumb/"):].split("?")[0]
            thumb = get_thumb_bytes(uuid)
            if thumb:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", len(thumb))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                try:
                    self.wfile.write(thumb)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_response(404)
                self.end_headers()
            return

        if parsed.path.startswith("/api/image/"):
            uuid = parsed.path[len("/api/image/"):].split("?")[0]
            img_bytes, mime = get_image_bytes(uuid)
            if img_bytes:
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", len(img_bytes))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                try:
                    self.wfile.write(img_bytes)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # browser navigated away before image finished loading
            else:
                self.send_response(404)
                self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/quit":
            _quit_requested.set()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == "/api/relaunch":
            _relaunch_event.set()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path.startswith("/api/copy-image/"):
            uuid = self.path[len("/api/copy-image/"):]
            ok = copy_image_to_clipboard(uuid)
            self.send_response(200 if ok else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}' if ok else b'{"ok":false}')
            return

        if self.path == "/api/copy":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                copy_to_clipboard(data.get("text", ""))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
            return

        self.send_response(404)
        self.end_headers()


# ── Browser focus helper ───────────────────────────────────────────────────────
def watch_for_new_items():
    """Poll Clipy data dir every 2s; push new clips via SSE."""
    known = {p.name for p in CLIPY_DATA_DIR.glob("*.data")}
    while True:
        time.sleep(2)
        try:
            current = {p.name for p in CLIPY_DATA_DIR.glob("*.data")}
            new_names = current - known
            known = current
            if not new_names:
                continue
            # Small delay so Clipy finishes writing before we parse
            time.sleep(0.5)
            new_paths = sorted(
                [CLIPY_DATA_DIR / n for n in new_names],
                key=lambda p: p.stat().st_mtime
            )
            for path in new_paths:
                item = parse_data_file(path)
                if item:
                    with _cache_lock:
                        if _items_cache is not None:
                            _items_cache.insert(0, item)
                    sse_broadcast("newitem", json.dumps(item, default=str))
        except Exception:
            pass


def focus_or_open(url):
    """On macOS, focus an existing ClipySearch tab; otherwise open a new one."""
    if sys.platform != "darwin":
        webbrowser.open(url)
        return
    # Try to find and focus an existing tab in Chrome or Safari
    script = """
set targetURL to "{url}"
set activated to false

-- Try Chrome
try
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                if URL of t starts with "http://127.0.0.1:57341" then
                    set active tab index of w to index of t
                    set index of w to 1
                    activate
                    set activated to true
                    exit repeat
                end if
            end repeat
            if activated then exit repeat
        end repeat
    end tell
end try

if not activated then
    -- Try Safari
    try
        tell application "Safari"
            repeat with w in windows
                repeat with t in tabs of w
                    if URL of t starts with "http://127.0.0.1:57341" then
                        set current tab of w to t
                        set index of w to 1
                        activate
                        set activated to true
                        exit repeat
                    end if
                end repeat
                if activated then exit repeat
            end repeat
        end tell
    end try
end if

if not activated then
    open location targetURL
end if
""".format(url=url)
    subprocess.run(["osascript", "-e", script], capture_output=True)


def port_in_use(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    from urllib.parse import quote
    initial_query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""

    url = f"http://127.0.0.1:{PORT}/"
    if initial_query:
        url += f"#{quote(initial_query)}"

    # Kill any existing instance so this terminal process becomes the main one
    kill_existing()

    write_pid()
    global _server_ref

    # Pre-load in background so the first page request is fast
    threading.Thread(target=get_items, daemon=True).start()
    threading.Thread(target=watch_for_new_items, daemon=True).start()

    server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)
    _server_ref = server

    # Install Ctrl-C handler — first press enters relaunch window, second force-quits
    def _sigint(sig, frame):
        _ctrl_c_count[0] += 1
        if _ctrl_c_count[0] >= 2:
            print("\nForce quit.")
            os._exit(0)
        _ctrl_c_event.set()

    signal.signal(signal.SIGINT, _sigint)

    print(f"ClipySearch  →  {url}")
    print("Press Ctrl-C to quit.")

    def open_browser():
        time.sleep(0.3)
        focus_or_open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    # Run server on a non-daemon thread so process stays alive during relaunch window
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = False
    server_thread.start()

    _ctrl_c_event.wait()   # block main thread until Ctrl-C

    sse_broadcast("shutdown", "relaunch")
    print("\nCtrl-C — click 'Relaunch' in the browser, or Ctrl-C again to force quit.")
    print("Waiting 30 s for browser relaunch…")

    _relaunch_event.wait(timeout=30)

    server.shutdown()
    server_thread.join(timeout=3)
    server.server_close()   # release the port before spawning new process

    if _relaunch_event.is_set():
        time.sleep(0.3)   # give OS a moment to fully release the socket
        subprocess.Popen([sys.executable] + sys.argv)
        print("Relaunching ClipySearch…")
    else:
        print("Timeout — exiting.")

    print("Bye.")


if __name__ == "__main__":
    main()
