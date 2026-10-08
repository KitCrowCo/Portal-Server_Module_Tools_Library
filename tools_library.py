# modules/tools_library/tools_library.py
"""
Internal utility distribution point. Each subfolder of ./data/tools_library/dist/ is one utility, self-describing via its own manifest.json: {"label": "...", "description": "...", "mode": "browser"|"download", "entry": "index.html"}
DIST_DIR is deliberately its own "dist" subfolder, not the module's data root
"browser" mode tools are served directly (open in a tab, no install) - entry is the file to load.
"download" mode tools are zipped whole and handed over with whatever instructions.md/requirements.txt they carry inside - install and run happens on the person's own machine, outside Portal Server entirely.
Nothing here is specific to any one utility - drop a folder in, it appears.
A tool can also come from a git repository (Gitea, GitHub, any git remote): it is cloned into its folder and updated with a fast-forward-only pull, so local edits are never overwritten.
The remote is recorded in ./data/tools_library/sources/<name>.json; credentials come from a Git Manager connection at pull time and are never written into the clone.
"""
import io, json, shutil, zipfile, asyncio, subprocess, os
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit, quote
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse

MODULE_META = {"label": "Tool Library", "icon": "&#x1F4E6;", "description": "Standalone utilities for hardware/tasks that don't run through the portal itself"}
router = APIRouter()
ENV = {}
UI = None
DIST_DIR = Path("./data/tools_library/dist")
SRC_DIR = Path("./data/tools_library/sources")
_GIT = shutil.which("git") or "git"

def init_module(environment: dict):
    global ENV, UI
    ENV.update(environment)
    UI = ENV["templates"].env.globals.get("UI")
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    SRC_DIR.mkdir(parents=True, exist_ok=True)

def _sanitize(name: str) -> str: return "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip("_") or "tool"
def _is_admin(request: Request) -> bool: return getattr(request.state.user, "role", "") == "admin"

def _manifest(folder: Path) -> dict:
    p = folder / "manifest.json"
    try: return json.loads(p.read_text()) if p.exists() else {"label": folder.name, "mode": "download"}
    except Exception: return {"label": folder.name, "mode": "download"}

def _list_tools() -> list:
    if not DIST_DIR.exists(): return []
    return sorted(({"name": d.name, **_manifest(d)} for d in DIST_DIR.iterdir() if d.is_dir()), key=lambda t: t["label"])

# --- Git sources ---

def _source(name: str) -> dict: return json.loads((SRC_DIR / f"{name}.json").read_text()) if (SRC_DIR / f"{name}.json").exists() else {}

def _git(args: list, cwd: Path = None, timeout: int = 300) -> tuple:
    r = subprocess.run([_GIT, *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return r.returncode, (r.stdout + r.stderr).strip()

def _git_conns() -> list:
    gm = ENV["tools"].get("git_manager")
    return gm.list_connections() if gm else []

def _remote(src: dict, with_auth: bool) -> str:
    """The repository URL; with_auth adds the Git Manager connection's user and token (used only on the command line of a clone or pull, never stored)."""
    repo, cid = src["repo"].strip(), src.get("connection_id", "")
    conn = ENV["tools"]["git_manager"].get_connection(cid) if cid else None
    if "://" not in repo:   # owner/name, resolved against the connection's server
        if not conn: raise ValueError("a repository given as owner/name needs a Git Manager connection")
        repo = f"""{conn["url"].rstrip("/")}/{repo.removesuffix(".git")}.git"""
    if not (with_auth and conn and conn.get("token")): return repo
    u = urlsplit(repo)
    return u._replace(netloc=f"""{quote(conn.get("user", ""), safe="")}:{quote(ENV["decrypt_token"](conn["token"]), safe="")}@{u.hostname}{f":{u.port}" if u.port else ""}""").geturl()

def _clone(name: str, src: dict) -> tuple:
    dest = DIST_DIR / name
    if dest.exists(): return False, f"{name} already exists - pick another folder name or delete it first"
    rc, out = _git(["clone", "--depth", "1", *(["--branch", src["branch"]] if src.get("branch") else []), _remote(src, True), str(dest)])
    if rc: shutil.rmtree(dest, ignore_errors=True); return False, out.replace(_remote(src, True), _remote(src, False))
    _git(["remote", "set-url", "origin", _remote(src, False)], dest)   # the clone keeps the plain URL; credentials are supplied per pull
    (SRC_DIR / f"{name}.json").write_text(json.dumps({**src, "added": datetime.utcnow().isoformat()}, indent=2))
    return True, out

def _pull(name: str) -> tuple:
    src, dest = _source(name), DIST_DIR / name
    if not src: return False, "not a git-backed tool"
    branch = src.get("branch") or _git(["rev-parse", "--abbrev-ref", "HEAD"], dest)[1]
    rc, out = _git(["pull", "--ff-only", _remote(src, True), branch], dest)   # fast-forward only: a tool folder edited locally reports the conflict instead of being overwritten
    return rc == 0, out.replace(_remote(src, True), _remote(src, False))

def _last_change(name: str) -> str:
    """Newest commit (git-backed tools) or newest file time, for the list."""
    folder = DIST_DIR / name
    if (folder / ".git").exists():
        rc, out = _git(["log", "-1", "--format=%cd|%h|%s", "--date=format:%Y-%m-%d %H:%M"], folder, timeout=10)
        if rc == 0 and out: d, h, msg = out.split("|", 2); return f"""{d} &middot; {UI.escape(h)} {UI.escape(msg[:60])}"""
    times = [f.stat().st_mtime for f in folder.rglob("*") if f.is_file() and ".git" not in f.parts]
    return datetime.fromtimestamp(max(times)).strftime("%Y-%m-%d %H:%M") if times else "empty"

# --- Rendering ---

def _status(html: str, ok: bool) -> str: return f"""<div style="font-size:.75rem;margin:.4rem 0;color:{"var(--accent)" if ok else "#ff5f5f"};white-space:pre-wrap">{html}</div>"""

def _body_html(admin: bool, note: str = "") -> str:
    rows = ""
    for t in _list_tools():
        action = f"""<a class="ui-btn" href="/module/tools_library/open/{t["name"]}/{t.get("entry","index.html")}" target="_blank">Open</a>""" if t.get("mode") == "browser" else f"""<a class="ui-btn" href="/module/tools_library/download/{t["name"]}">Download</a>"""
        src = _source(t["name"])
        pull_btn = f"""<button class="btn-icon" title="Update from {UI.escape(src.get("repo", ""))} (fast-forward only)" hx-post="/module/tools_library/pull/{t["name"]}" hx-target="#tl-list" hx-swap="innerHTML">&#x21BB;</button>""" if admin and src else ""
        del_btn = f"""<button class="btn-icon" style="color:#ff5f5f" hx-post="/module/tools_library/delete/{t["name"]}" hx-target="#tl-list" hx-swap="innerHTML" hx-confirm="Delete {UI.escape(t["label"])}?">&#x2715;</button>""" if admin else ""
        version = f"""<span style="font-size:.7rem;color:var(--text_muted)"> v{UI.escape(str(t["version"]))}</span>""" if t.get("version") else ""
        origin = f""" &middot; git: {UI.escape(src["repo"])}{"@" + UI.escape(src["branch"]) if src.get("branch") else ""}""" if src else ""
        rows += f"""<div class="glass" style="padding:.8rem;margin-bottom:.5rem;display:flex;align-items:center;gap:.6rem"><div style="flex:1;min-width:0"><b>{UI.escape(t["label"])}</b>{version}<div style="font-size:.8rem;color:var(--text_muted);margin:.2rem 0">{UI.escape(t.get("description",""))}</div><div style="font-size:.68rem;color:var(--text_muted)">Last change: {_last_change(t["name"])}{origin}</div></div>{action}{pull_btn}{del_btn}</div>"""
    if not admin: return rows or "<p>None published yet.</p>"
    conn_opts = "".join(f"""<option value="{UI.escape(c["_id"])}">{UI.escape(c.get("label") or c["_id"])} ({UI.escape(c.get("url", ""))})</option>""" for c in _git_conns())
    upload_html = f"""<details class="glass" style="padding:.8rem;margin-top:1rem">
                           <summary style="cursor:pointer;font-size:.85rem;color:var(--text_muted)">+ Upload new tool</summary>
                           <form hx-post="/module/tools_library/upload" hx-target="#tl-list" hx-swap="innerHTML" hx-encoding="multipart/form-data" style="display:flex;flex-direction:column;gap:.5rem;margin-top:.6rem">
                               <label style="font-size:.75rem;color:var(--text_muted)">Folder name (used in URLs - letters, numbers, - and _ only)<input type="text" name="name" class="module-select" required></label>
                               <label style="font-size:.75rem;color:var(--text_muted)">Zip file (its contents become the tool's folder - include manifest.json inside)<input type="file" name="archive" accept=".zip" required></label>
                               <button type="submit" class="button">Upload</button>
                           </form>
                       </details>
                       <details class="glass" style="padding:.8rem;margin-top:.5rem">
                           <summary style="cursor:pointer;font-size:.85rem;color:var(--text_muted)">+ Add from a git repository</summary>
                           <form hx-post="/module/tools_library/git_add" hx-target="#tl-list" hx-swap="innerHTML" style="display:flex;flex-direction:column;gap:.5rem;margin-top:.6rem">
                               <label style="font-size:.75rem;color:var(--text_muted)">Folder name<input type="text" name="name" class="module-select" required></label>
                               <label style="font-size:.75rem;color:var(--text_muted)">Repository (full URL, or owner/name on the connection's server)<input type="text" name="repo" class="module-select" placeholder="https://gitea.local/me/tool.git  or  me/tool" required></label>
                               <label style="font-size:.75rem;color:var(--text_muted)">Branch (blank = the repository's default)<input type="text" name="branch" class="module-select"></label>
                               <label style="font-size:.75rem;color:var(--text_muted)">Credentials (Git Manager connection; blank = public repository)<select name="connection_id" class="module-select"><option value="">(none)</option>{conn_opts}</select></label>
                               <button type="submit" class="button">Clone</button>
                           </form>
                       </details>"""
    return note + (rows or "<p>None published yet.</p>") + upload_html

# --- Routes ---

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return HTMLResponse(f"""<div style="max-width:40rem;margin:0 auto;padding:1.5rem"><h3>Internal Tool Downloads</h3><div id="tl-list">{_body_html(_is_admin(request))}</div></div>""")

@router.get("/open/{name}/{path:path}")
async def open_asset(name: str, path: str):
    p = DIST_DIR / name / path
    if not p.is_file() or ".git" in Path(path).parts: raise HTTPException(404)
    return FileResponse(p)

@router.get("/download/{name}")
async def download(name: str):
    folder = DIST_DIR / name
    if not folder.is_dir(): raise HTTPException(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folder.rglob("*"):
            if f.is_file() and ".git" not in f.relative_to(folder).parts: zf.write(f, f.relative_to(folder))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f"""attachment; filename="{name}.zip\""""})

@router.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, name: str = Form(...), archive: UploadFile = File(...)):
    if not _is_admin(request): return HTMLResponse("Unauthorized", status_code=403)
    dest = DIST_DIR / _sanitize(name)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(await archive.read())) as zf:
        for member in zf.namelist():
            if ".." in Path(member).parts: raise HTTPException(400, "Zip contains path traversal entries")
        zf.extractall(dest)
    return HTMLResponse(_body_html(True))

@router.post("/git_add", response_class=HTMLResponse)
async def git_add(request: Request, name: str = Form(...), repo: str = Form(...), branch: str = Form(""), connection_id: str = Form("")):
    if not _is_admin(request): return HTMLResponse("Unauthorized", status_code=403)
    try: ok, out = await asyncio.to_thread(_clone, _sanitize(name), {"repo": repo.strip(), "branch": branch.strip(), "connection_id": connection_id})
    except (ValueError, subprocess.TimeoutExpired) as e: ok, out = False, str(e)
    return HTMLResponse(_body_html(True, _status(f"&#x2713; Cloned into {UI.escape(_sanitize(name))}" if ok else f"&#x26A0; Clone failed: {UI.escape(out[-600:])}", ok)))

@router.post("/pull/{name}", response_class=HTMLResponse)
async def pull(name: str, request: Request):
    if not _is_admin(request): return HTMLResponse("Unauthorized", status_code=403)
    try: ok, out = await asyncio.to_thread(_pull, _sanitize(name))
    except (ValueError, subprocess.TimeoutExpired) as e: ok, out = False, str(e)
    return HTMLResponse(_body_html(True, _status(f"""{"&#x2713;" if ok else "&#x26A0;"} {UI.escape(name)}: {UI.escape(out[-600:]) or "up to date"}""", ok)))

@router.post("/delete/{name}", response_class=HTMLResponse)
async def delete_tool(name: str, request: Request):
    if not _is_admin(request): return HTMLResponse("Unauthorized", status_code=403)
    shutil.rmtree(DIST_DIR / name, ignore_errors=True)
    (SRC_DIR / f"{_sanitize(name)}.json").unlink(missing_ok=True)
    return HTMLResponse(_body_html(True))
