import os
import re
import uuid
import json
import time
import threading
import requests
from urllib.parse import unquote
from flask import Flask, render_template_string, jsonify, request, Response
from bs4 import BeautifulSoup

app = Flask(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
IGDB_CLIENT_ID     = "iog3ow9pb6xukvsk3kqr5uguoz5wix"
IGDB_CLIENT_SECRET = "j1jqhvenqnq5a2eacqjdiu81j032qd"
ARCHIVE_URL        = "https://archive.org/download/psp-cso-collection"
DOWNLOAD_DIR       = "/sdcard/rom/"

# ─── State ────────────────────────────────────────────────────────────────────
downloads    = {}   # { dl_id: { progress, status, filename, error } }
games_cache  = None
igdb_token   = None
covers_cache = {}
token_lock   = threading.Lock()

# ─── IGDB helpers ─────────────────────────────────────────────────────────────
def get_igdb_token():
    global igdb_token
    with token_lock:
        try:
            r = requests.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id":     IGDB_CLIENT_ID,
                    "client_secret": IGDB_CLIENT_SECRET,
                    "grant_type":    "client_credentials",
                },
                timeout=10,
            )
            igdb_token = r.json().get("access_token")
        except Exception as e:
            print(f"[IGDB] Token error: {e}")
    return igdb_token


def igdb_cover(game_name: str) -> str | None:
    global igdb_token
    if game_name in covers_cache:
        return covers_cache[game_name]

    # Ensure we have a token (thread-safe)
    if not igdb_token:
        get_igdb_token()
    if not igdb_token:
        covers_cache[game_name] = None
        return None

    # Strip region/version tags for cleaner search
    clean = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", game_name).strip()
    clean = re.sub(r"[_-]", " ", clean).strip()

    headers = {
        "Client-ID":     IGDB_CLIENT_ID,
        "Authorization": f"Bearer {igdb_token}",
        "Accept":        "application/json",
        "Content-Type":  "text/plain",
    }

    # Search without platform filter first to maximize cover hits
    body = f'search "{clean}"; fields cover.url,name,platforms; limit 5;'

    try:
        r = requests.post(
            "https://api.igdb.com/v4/games",
            headers=headers, data=body.encode("utf-8"), timeout=10
        )
        if r.status_code == 401:
            # Token expired, refresh and retry once
            get_igdb_token()
            headers["Authorization"] = f"Bearer {igdb_token}"
            r = requests.post(
                "https://api.igdb.com/v4/games",
                headers=headers, data=body.encode("utf-8"), timeout=10
            )

        data = r.json()
        if not isinstance(data, list):
            covers_cache[game_name] = None
            return None

        # Prefer PSP (platform 38) result, else take first with a cover
        best = None
        for g in data:
            if not g.get("cover"):
                continue
            platforms = g.get("platforms", [])
            if 38 in platforms:
                best = g
                break
            if best is None:
                best = g

        if best and best.get("cover"):
            raw = best["cover"]["url"]
            # Fix protocol-relative URL
            if raw.startswith("//"):
                raw = "https:" + raw
            # Upgrade thumbnail to big cover
            url = raw.replace("t_thumb", "t_cover_big")
            covers_cache[game_name] = url
            return url

    except Exception as e:
        print(f"[IGDB] Cover error for '{game_name}': {e}")

    covers_cache[game_name] = None
    return None


# ─── Archive.org helpers ──────────────────────────────────────────────────────
def clean_name(filename: str) -> str:
    name = unquote(filename)                          # Pro%20Evolution → Pro Evolution
    name = re.sub(r"\.(cso|iso|CSO|ISO)$", "", name)
    name = re.sub(r"\s*\(.*?\)", "", name)
    name = re.sub(r"\s*\[.*?\]", "", name)
    return name.strip()


def fetch_games() -> list[dict]:
    global games_cache
    if games_cache is not None:
        return games_cache
    try:
        r = requests.get(ARCHIVE_URL + "/", timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        files = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".cso") or href.lower().endswith(".iso"):
                filename = href.split("/")[-1]
                files.append({
                    "id":       str(uuid.uuid5(uuid.NAMESPACE_URL, href)),
                    "filename": filename,
                    "name":     clean_name(filename),
                    "url":      ARCHIVE_URL + "/" + filename,
                    "cover":    None,
                })
        games_cache = files
        return files
    except Exception as e:
        return []


# ─── Background download ──────────────────────────────────────────────────────
def do_download(dl_id: str, url: str, filename: str):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest = os.path.join(DOWNLOAD_DIR, filename)
    try:
        downloads[dl_id]["status"] = "downloading"
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done  = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            downloads[dl_id]["progress"] = round(done / total * 100, 1)
                        else:
                            downloads[dl_id]["progress"] = -1   # unknown size
        downloads[dl_id]["status"]   = "done"
        downloads[dl_id]["progress"] = 100
    except Exception as e:
        downloads[dl_id]["status"] = "error"
        downloads[dl_id]["error"]  = str(e)


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/games")
def api_games():
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per", 40))
    search   = request.args.get("q", "").lower()
    games    = fetch_games()
    if search:
        games = [g for g in games if search in g["name"].lower()]
    total  = len(games)
    start  = (page - 1) * per_page
    chunk  = games[start:start + per_page]
    # fetch covers in threads
    def load_cover(g):
        if g["cover"] is None:
            g["cover"] = igdb_cover(g["name"])
    threads = [threading.Thread(target=load_cover, args=(g,)) for g in chunk]
    for t in threads: t.start()
    for t in threads: t.join()
    return jsonify({"games": chunk, "total": total, "page": page, "per": per_page})


@app.route("/api/download", methods=["POST"])
def api_download():
    data     = request.json
    url      = data.get("url")
    filename = data.get("filename")
    dl_id    = str(uuid.uuid4())
    downloads[dl_id] = {"progress": 0, "status": "starting", "filename": filename, "error": None}
    t = threading.Thread(target=do_download, args=(dl_id, url, filename), daemon=True)
    t.start()
    return jsonify({"dl_id": dl_id})


@app.route("/api/progress/<dl_id>")
def api_progress(dl_id):
    def stream():
        while True:
            info = downloads.get(dl_id, {})
            yield f"data: {json.dumps(info)}\n\n"
            if info.get("status") in ("done", "error"):
                break
            time.sleep(0.5)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── HTML Template ────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>PSP Vault</title>
<link href="https://fonts.googleapis.com/css2?family=Oxanium:wght@300;400;600;800&family=Inter:wght@300;400;500&display=swap" rel="stylesheet"/>
<style>
  :root {
    --bg:      #07080f;
    --surface: #0f1118;
    --card:    #141722;
    --border:  rgba(120,140,255,.13);
    --glow:    #5b6ef5;
    --glow2:   #a855f7;
    --text:    #e8eaf6;
    --muted:   #636880;
    --accent:  #6c7eff;
    --green:   #22c55e;
    --red:     #ef4444;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Animated mesh background */
  body::before {
    content: '';
    position: fixed; inset: 0; z-index: 0; pointer-events: none;
    background:
      radial-gradient(ellipse 80% 60% at 10% 20%, rgba(91,110,245,.12) 0%, transparent 70%),
      radial-gradient(ellipse 60% 40% at 90% 80%, rgba(168,85,247,.10) 0%, transparent 60%),
      radial-gradient(ellipse 40% 30% at 50% 50%, rgba(108,126,255,.05) 0%, transparent 60%);
  }

  /* ── NAV ── */
  nav {
    position: sticky; top: 0; z-index: 100;
    background: rgba(7,8,15,.85);
    backdrop-filter: blur(24px) saturate(160%);
    border-bottom: 1px solid var(--border);
    padding: 0 2rem;
    display: flex; align-items: center; gap: 1.5rem; height: 64px;
  }
  .logo {
    font-family: 'Oxanium', sans-serif;
    font-size: 1.4rem; font-weight: 800; letter-spacing: .06em;
    background: linear-gradient(135deg, var(--glow), var(--glow2));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    white-space: nowrap;
  }
  .logo span { font-weight: 300; opacity: .7; }
  nav .sub { font-size: .75rem; color: var(--muted); margin-top: .15rem; }

  .search-wrap {
    flex: 1; max-width: 480px; margin-left: auto;
    position: relative;
  }
  .search-wrap svg {
    position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
    color: var(--muted); pointer-events: none;
  }
  #searchInput {
    width: 100%;
    background: rgba(255,255,255,.04);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: .55rem 1.2rem .55rem 2.6rem;
    color: var(--text);
    font-size: .88rem; font-family: 'Inter', sans-serif;
    outline: none;
    transition: border .2s, box-shadow .2s;
  }
  #searchInput:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(108,126,255,.18);
  }
  #searchInput::placeholder { color: var(--muted); }

  /* ── HERO ── */
  .hero {
    position: relative; z-index: 1;
    padding: 3.5rem 2rem 2rem;
    text-align: center;
  }
  .hero h1 {
    font-family: 'Oxanium', sans-serif;
    font-size: clamp(2rem, 5vw, 3.5rem);
    font-weight: 800; letter-spacing: .04em; line-height: 1.1;
    background: linear-gradient(135deg, #fff 30%, var(--glow2));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    margin-bottom: .6rem;
  }
  .hero p { color: var(--muted); font-size: .95rem; }

  /* ── STATS BAR ── */
  .stats {
    position: relative; z-index: 1;
    display: flex; justify-content: center; gap: 2rem;
    margin: 1rem auto 2rem; flex-wrap: wrap;
  }
  .stat {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: .7rem 1.6rem;
    font-family: 'Oxanium', sans-serif;
    text-align: center;
  }
  .stat .val { font-size: 1.4rem; font-weight: 700; color: var(--accent); }
  .stat .lbl { font-size: .68rem; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }

  /* ── GRID ── */
  .grid-wrap {
    position: relative; z-index: 1;
    max-width: 1500px; margin: 0 auto;
    padding: 0 1.5rem 5rem;
  }
  #grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(175px, 1fr));
    gap: 1.25rem;
  }

  /* ── GAME CARD ── */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 20px;
    overflow: hidden;
    cursor: pointer;
    transition: transform .22s cubic-bezier(.34,1.56,.64,1), box-shadow .22s, border-color .22s;
    position: relative;
  }
  .card:hover {
    transform: translateY(-6px) scale(1.02);
    border-color: var(--accent);
    box-shadow: 0 12px 40px rgba(91,110,245,.22), 0 0 0 1px rgba(108,126,255,.18);
  }
  .card-img {
    width: 100%; aspect-ratio: 3/4;
    object-fit: cover;
    background: linear-gradient(135deg, #1a1d2e, #0f1118);
    display: block;
  }
  .card-img.placeholder {
    display: flex; align-items: center; justify-content: center;
    font-size: 2.5rem;
  }
  .card-body { padding: .75rem .9rem .85rem; }
  .card-title {
    font-family: 'Oxanium', sans-serif;
    font-size: .78rem; font-weight: 600;
    line-height: 1.3;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden;
    color: var(--text);
  }
  .card-badge {
    display: inline-block;
    margin-top: .4rem;
    background: rgba(108,126,255,.15);
    color: var(--accent);
    font-size: .62rem; font-weight: 600; letter-spacing: .07em;
    text-transform: uppercase;
    padding: .15rem .55rem;
    border-radius: 999px;
    border: 1px solid rgba(108,126,255,.22);
  }

  /* ── SKELETON ── */
  .skeleton { animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .skel-img  { background: #1d2033; width:100%; aspect-ratio:3/4; border-radius:0; }
  .skel-text { background: #1d2033; height:12px; border-radius:6px; margin:.7rem .9rem .4rem; }
  .skel-text2{ background: #1d2033; height:10px; border-radius:6px; margin:0 .9rem .8rem; width:60%; }

  /* ── PAGINATION ── */
  .pagination {
    display: flex; justify-content: center; gap: .5rem; margin: 3rem 0 1rem;
    flex-wrap: wrap;
  }
  .page-btn {
    background: var(--card); border: 1px solid var(--border);
    color: var(--text); border-radius: 12px;
    padding: .5rem .95rem; font-size: .82rem; font-family: 'Oxanium',sans-serif;
    cursor: pointer; transition: all .18s;
  }
  .page-btn:hover { border-color: var(--accent); color: var(--accent); }
  .page-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .page-btn:disabled { opacity: .3; cursor: default; }

  /* ── MODAL ── */
  .modal-backdrop {
    position: fixed; inset: 0; z-index: 200;
    background: rgba(0,0,0,.75);
    backdrop-filter: blur(8px);
    display: flex; align-items: center; justify-content: center;
    padding: 1rem;
    opacity: 0; pointer-events: none;
    transition: opacity .25s;
  }
  .modal-backdrop.open { opacity: 1; pointer-events: all; }
  .modal {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 28px;
    width: 100%; max-width: 580px;
    overflow: hidden;
    transform: scale(.94) translateY(20px);
    transition: transform .3s cubic-bezier(.34,1.56,.64,1);
    position: relative;
  }
  .modal-backdrop.open .modal { transform: scale(1) translateY(0); }

  .modal-cover {
    width: 100%; height: 220px; object-fit: cover;
    background: linear-gradient(135deg, #1a1d2e, #0f1118);
    display: block;
  }
  .modal-overlay {
    position: absolute; top: 0; left: 0; right: 0; height: 220px;
    background: linear-gradient(to bottom, transparent 40%, var(--surface));
  }
  .modal-body { padding: 1.4rem 1.8rem 1.8rem; }
  .modal-title {
    font-family: 'Oxanium',sans-serif;
    font-size: 1.4rem; font-weight: 800; line-height: 1.2;
    margin-bottom: .35rem;
  }
  .modal-file { color: var(--muted); font-size: .78rem; margin-bottom: 1.2rem; }

  .btn-dl {
    width: 100%;
    background: linear-gradient(135deg, var(--glow), var(--glow2));
    color: #fff; border: none; border-radius: 999px;
    padding: .9rem 1.5rem;
    font-family: 'Oxanium',sans-serif; font-size: 1rem; font-weight: 700;
    letter-spacing: .06em; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: .6rem;
    transition: opacity .18s, transform .18s;
    box-shadow: 0 4px 20px rgba(91,110,245,.35);
  }
  .btn-dl:hover:not(:disabled) { opacity: .88; transform: scale(1.02); }
  .btn-dl:disabled { opacity: .5; cursor: not-allowed; transform: none; }

  /* ── PROGRESS BAR ── */
  .progress-wrap {
    margin-top: 1.2rem;
    display: none;
  }
  .progress-wrap.visible { display: block; }
  .progress-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: .5rem;
  }
  .progress-status { font-size: .82rem; color: var(--muted); font-family: 'Oxanium',sans-serif; }
  .progress-pct { font-size: .82rem; font-weight: 700; color: var(--accent); font-family: 'Oxanium',sans-serif; }
  .progress-track {
    height: 8px; background: rgba(255,255,255,.07);
    border-radius: 999px; overflow: hidden;
  }
  .progress-bar {
    height: 100%;
    background: linear-gradient(90deg, var(--glow), var(--glow2));
    border-radius: 999px;
    width: 0%;
    transition: width .4s ease;
    position: relative;
  }
  .progress-bar::after {
    content: ''; position: absolute; inset: 0;
    background: linear-gradient(90deg, transparent 0%, rgba(255,255,255,.3) 50%, transparent 100%);
    animation: shimmer 1.5s infinite;
  }
  @keyframes shimmer { 0%{transform:translateX(-100%)} 100%{transform:translateX(100%)} }

  .done-msg {
    display: none; margin-top: .9rem;
    background: rgba(34,197,94,.1);
    border: 1px solid rgba(34,197,94,.25);
    border-radius: 12px; padding: .7rem 1rem;
    color: var(--green); font-size: .82rem; font-family: 'Oxanium',sans-serif;
    text-align: center;
  }
  .done-msg.visible { display: block; }
  .err-msg {
    display: none; margin-top: .9rem;
    background: rgba(239,68,68,.1);
    border: 1px solid rgba(239,68,68,.25);
    border-radius: 12px; padding: .7rem 1rem;
    color: var(--red); font-size: .82rem;
    text-align: center;
  }
  .err-msg.visible { display: block; }

  .close-btn {
    position: absolute; top: 14px; right: 14px;
    width: 34px; height: 34px;
    background: rgba(0,0,0,.45); border: 1px solid var(--border);
    border-radius: 50%; color: var(--text);
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; font-size: 1rem;
    transition: background .18s;
    z-index: 10;
  }
  .close-btn:hover { background: rgba(255,255,255,.1); }

  /* ── TOAST ── */
  #toast {
    position: fixed; bottom: 2rem; right: 2rem; z-index: 999;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; padding: .85rem 1.3rem;
    font-size: .85rem; color: var(--text);
    box-shadow: 0 8px 32px rgba(0,0,0,.4);
    transform: translateY(20px); opacity: 0;
    transition: all .3s cubic-bezier(.34,1.56,.64,1);
    pointer-events: none;
  }
  #toast.show { transform: translateY(0); opacity: 1; }

  /* ── LOADING SPINNER ── */
  .spinner {
    display: none; justify-content: center; align-items: center;
    padding: 5rem 0; flex-direction: column; gap: 1rem;
  }
  .spinner.visible { display: flex; }
  .spin {
    width: 44px; height: 44px; border-radius: 50%;
    border: 3px solid rgba(255,255,255,.06);
    border-top-color: var(--accent);
    animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner p { color: var(--muted); font-size: .85rem; font-family: 'Oxanium',sans-serif; }

  /* ── EMPTY STATE ── */
  .empty {
    text-align: center; padding: 5rem 2rem; display: none;
  }
  .empty.visible { display: block; }
  .empty .icon { font-size: 3rem; margin-bottom: 1rem; }
  .empty h3 { font-family: 'Oxanium',sans-serif; font-size: 1.2rem; margin-bottom: .5rem; }
  .empty p { color: var(--muted); font-size: .88rem; }

  @media(max-width:600px) {
    nav { padding: 0 1rem; }
    .logo { font-size: 1.1rem; }
    .hero { padding: 2rem 1rem 1rem; }
    .grid-wrap { padding: 0 1rem 4rem; }
    #grid { grid-template-columns: repeat(auto-fill,minmax(140px,1fr)); gap: 1rem; }
  }
</style>
</head>
<body>

<!-- NAV -->
<nav>
  <div>
    <div class="logo">PSP<span>Vault</span></div>
    <div class="sub">Archive.org · IGDB Covers</div>
  </div>
  <div class="search-wrap">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <input id="searchInput" type="text" placeholder="Rechercher un jeu PSP…" autocomplete="off"/>
  </div>
</nav>

<!-- HERO -->
<div class="hero">
  <h1>PSP Game Vault</h1>
  <p>Télécharge tes jeux PSP directement depuis Archive.org avec couvertures IGDB</p>
</div>

<!-- STATS -->
<div class="stats">
  <div class="stat">
    <div class="val" id="statTotal">—</div>
    <div class="lbl">Jeux disponibles</div>
  </div>
  <div class="stat">
    <div class="val" id="statPage">—</div>
    <div class="lbl">Page actuelle</div>
  </div>
  <div class="stat">
    <div class="val">CSO / ISO</div>
    <div class="lbl">Formats</div>
  </div>
</div>

<!-- GRID -->
<div class="grid-wrap">
  <div class="spinner visible" id="spinner">
    <div class="spin"></div>
    <p>Chargement des jeux…</p>
  </div>
  <div class="empty" id="empty">
    <div class="icon">🎮</div>
    <h3>Aucun jeu trouvé</h3>
    <p>Essaie un autre terme de recherche</p>
  </div>
  <div id="grid"></div>
  <div class="pagination" id="pagination"></div>
</div>

<!-- MODAL -->
<div class="modal-backdrop" id="modalBackdrop">
  <div class="modal" id="modal">
    <button class="close-btn" id="closeModal">✕</button>
    <img id="modalCover" class="modal-cover" src="" alt="cover"/>
    <div class="modal-overlay"></div>
    <div class="modal-body">
      <div class="modal-title" id="modalTitle"></div>
      <div class="modal-file" id="modalFile"></div>
      <button class="btn-dl" id="btnDownload">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
        Télécharger vers /sdcard/rom/
      </button>
      <div class="progress-wrap" id="progressWrap">
        <div class="progress-header">
          <span class="progress-status" id="progressStatus">Téléchargement…</span>
          <span class="progress-pct" id="progressPct">0%</span>
        </div>
        <div class="progress-track">
          <div class="progress-bar" id="progressBar"></div>
        </div>
      </div>
      <div class="done-msg" id="doneMsg">✅ Fichier enregistré dans /sdcard/rom/ !</div>
      <div class="err-msg" id="errMsg"></div>
    </div>
  </div>
</div>

<!-- TOAST -->
<div id="toast"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let currentPage = 1;
let currentGame = null;
let searchTimer = null;
let currentDlId  = null;
let evtSource    = null;

// ── Fetch & render games ───────────────────────────────────────────────────
async function loadGames(page = 1, q = '') {
  document.getElementById('spinner').classList.add('visible');
  document.getElementById('empty').classList.remove('visible');
  document.getElementById('grid').innerHTML = '';
  document.getElementById('pagination').innerHTML = '';

  try {
    const res  = await fetch(`/api/games?page=${page}&per=40&q=${encodeURIComponent(q)}`);
    const data = await res.json();

    document.getElementById('spinner').classList.remove('visible');
    document.getElementById('statTotal').textContent = data.total.toLocaleString();
    document.getElementById('statPage').textContent  = `${data.page} / ${Math.ceil(data.total / data.per)}`;

    if (!data.games.length) {
      document.getElementById('empty').classList.add('visible');
      return;
    }

    renderGrid(data.games);
    renderPagination(data.page, Math.ceil(data.total / data.per));
  } catch (e) {
    document.getElementById('spinner').classList.remove('visible');
    toast('Erreur de chargement 😢');
  }
}

function renderGrid(games) {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  games.forEach((g, i) => {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.animationDelay = `${i * 0.03}s`;
    card.innerHTML = g.cover
      ? `<img class="card-img" src="${g.cover}" alt="${escHtml(g.name)}" loading="lazy" onerror="this.outerHTML='<div class=\\'card-img placeholder\\'>🎮</div>'">`
      : `<div class="card-img placeholder">🎮</div>`;
    card.innerHTML += `
      <div class="card-body">
        <div class="card-title">${escHtml(g.name)}</div>
        <span class="card-badge">${g.filename.split('.').pop().toUpperCase()}</span>
      </div>`;
    card.addEventListener('click', () => openModal(g));
    grid.appendChild(card);
  });
}

function renderPagination(current, total) {
  const wrap = document.getElementById('pagination');
  const range = pagRange(current, total);
  range.forEach(p => {
    const btn = document.createElement('button');
    btn.className = 'page-btn' + (p === current ? ' active' : '');
    btn.textContent = p === '…' ? '…' : p;
    if (p === '…') btn.disabled = true;
    else btn.addEventListener('click', () => { currentPage = p; loadGames(p, document.getElementById('searchInput').value.trim()); });
    wrap.appendChild(btn);
  });
}

function pagRange(c, t) {
  if (t <= 7) return Array.from({length: t}, (_, i) => i + 1);
  if (c <= 4)  return [1,2,3,4,5,'…',t];
  if (c >= t-3)return [1,'…',t-4,t-3,t-2,t-1,t];
  return [1,'…',c-1,c,c+1,'…',t];
}

// ── Modal ──────────────────────────────────────────────────────────────────
function openModal(game) {
  currentGame = game;
  currentDlId  = null;
  if (evtSource) { evtSource.close(); evtSource = null; }

  document.getElementById('modalTitle').textContent = game.name;
  document.getElementById('modalFile').textContent  = game.filename;
  const cover = document.getElementById('modalCover');
  cover.src = game.cover || '';
  cover.style.display = game.cover ? 'block' : 'none';

  document.getElementById('btnDownload').disabled = false;
  document.getElementById('btnDownload').innerHTML = `
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
      <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
      <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
    </svg>
    Télécharger vers /sdcard/rom/`;
  document.getElementById('progressWrap').classList.remove('visible');
  document.getElementById('progressBar').style.width = '0%';
  document.getElementById('progressPct').textContent = '0%';
  document.getElementById('doneMsg').classList.remove('visible');
  document.getElementById('errMsg').classList.remove('visible');

  document.getElementById('modalBackdrop').classList.add('open');
}

document.getElementById('closeModal').addEventListener('click', closeModal);
document.getElementById('modalBackdrop').addEventListener('click', e => {
  if (e.target === document.getElementById('modalBackdrop')) closeModal();
});
function closeModal() {
  document.getElementById('modalBackdrop').classList.remove('open');
  if (evtSource) { evtSource.close(); evtSource = null; }
}

// ── Download ───────────────────────────────────────────────────────────────
document.getElementById('btnDownload').addEventListener('click', async () => {
  if (!currentGame) return;
  const btn = document.getElementById('btnDownload');
  btn.disabled = true;
  btn.innerHTML = '<div class="spin" style="width:18px;height:18px;border-width:2px;margin:0"></div> Démarrage…';

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url: currentGame.url, filename: currentGame.filename })
    });
    const { dl_id } = await res.json();
    currentDlId = dl_id;
    startProgress(dl_id);
  } catch (e) {
    btn.disabled = false;
    toast('Erreur au démarrage du téléchargement');
  }
});

function startProgress(dlId) {
  document.getElementById('progressWrap').classList.add('visible');
  document.getElementById('doneMsg').classList.remove('visible');
  document.getElementById('errMsg').classList.remove('visible');

  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/api/progress/${dlId}`);

  evtSource.onmessage = e => {
    const info = JSON.parse(e.data);
    const pct  = info.progress < 0 ? null : info.progress;

    if (pct !== null) {
      document.getElementById('progressBar').style.width = pct + '%';
      document.getElementById('progressPct').textContent = pct + '%';
    } else {
      document.getElementById('progressPct').textContent = '…';
    }

    const statusMap = { starting: 'Démarrage…', downloading: 'Téléchargement…', done: 'Terminé !', error: 'Erreur' };
    document.getElementById('progressStatus').textContent = statusMap[info.status] || info.status;

    if (info.status === 'done') {
      evtSource.close();
      document.getElementById('progressBar').style.width = '100%';
      document.getElementById('progressPct').textContent = '100%';
      document.getElementById('doneMsg').classList.add('visible');
      document.getElementById('btnDownload').disabled = true;
      document.getElementById('btnDownload').innerHTML = '✅ Téléchargé';
      toast('🎮 ' + currentGame.name + ' téléchargé !');
    } else if (info.status === 'error') {
      evtSource.close();
      const err = document.getElementById('errMsg');
      err.textContent = '❌ Erreur : ' + (info.error || 'inconnue');
      err.classList.add('visible');
      document.getElementById('btnDownload').disabled = false;
      document.getElementById('btnDownload').innerHTML = 'Réessayer';
    }
  };
}

// ── Search ────────────────────────────────────────────────────────────────
document.getElementById('searchInput').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    currentPage = 1;
    loadGames(1, e.target.value.trim());
  }, 450);
});

// ── Toast ─────────────────────────────────────────────────────────────────
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3200);
}

// ── Util ──────────────────────────────────────────────────────────────────
function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Init ──────────────────────────────────────────────────────────────────
loadGames(1);
</script>
</body>
</html>"""


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
