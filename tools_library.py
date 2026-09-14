# modules/tools_library/tools_library.py
"""
Internal utility distribution point. Each subfolder of ./data/tools_library/dist/ is one utility, self-describing via its own manifest.json: {"label": "...", "description": "...", "mode": "browser"|"download", "entry": "index.html"}
DIST_DIR is deliberately its own "dist" subfolder, not the module's data root
"browser" mode tools are served directly (open in a tab, no install) - entry is the file to load.
"download" mode tools are zipped whole and handed over with whatever instructions.md/requirements.txt they carry inside - install and run happens on the person's own machine, outside Portal Server entirely.
Nothing here is specific to any one utility - drop a folder in, it appears.
"""
import io, json, shutil, zipfile
from pathlib import Path
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse

MODULE_META = {"label": "Tool Library", "icon": "&#x1F4E6;", "description": "Standalone utilities for hardware/tasks that don't run through the portal itself"}
router = APIRouter()
ENV = {}
UI = None
DIST_DIR = Path("./data/tools_library/dist")

def init_module(environment: dict):
    global ENV, UI
    ENV.update(environment)
    UI = ENV["templates"].env.globals.get("UI")
    DIST_DIR.mkdir(parents=True, exist_ok=True)

def _sanitize(name: str) -> str: return "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip("_") or "tool"
def _is_admin(request: Request) -> bool: return getattr(request.state.user, "role", "") == "admin"

def _manifest(folder: Path) -> dict:
    p = folder / "manifest.json"
    try: return json.loads(p.read_text()) if p.exists() else {"label": folder.name, "mode": "download"}
    except Exception: return {"label": folder.name, "mode": "download"}

def _list_tools() -> list:
    if not DIST_DIR.exists(): return []
    return sorted(({"name": d.name, **_manifest(d)} for d in DIST_DIR.iterdir() if d.is_dir()), key=lambda t: t["label"])

def _body_html(admin: bool) -> str:
    rows = ""
    for t in _list_tools():
        action = f'<a class="ui-btn" href="/module/tools_library/open/{t["name"]}/{t.get("entry","index.html")}" target="_blank">Open</a>' if t.get("mode") == "browser" else f'<a class="ui-btn" href="/module/tools_library/download/{t["name"]}">Download</a>'
        del_btn = f'<button class="btn-icon" style="color:#ff5f5f" hx-post="/module/tools_library/delete/{t["name"]}" hx-target="#tl-list" hx-swap="innerHTML" hx-confirm="Delete {UI.escape(t["label"])}?">&#x2715;</button>' if admin else ""
        rows += f'<div class="glass" style="padding:.8rem;margin-bottom:.5rem;display:flex;align-items:center;gap:.6rem"><div style="flex:1"><b>{UI.escape(t["label"])}</b><div style="font-size:.8rem;color:var(--text_muted);margin:.2rem 0">{UI.escape(t.get("description",""))}</div></div>{action}{del_btn}</div>'
    upload_html = f"""<details class="glass" style="padding:.8rem;margin-top:1rem">
                           <summary style="cursor:pointer;font-size:.85rem;color:var(--text_muted)">+ Upload new tool</summary>
                           <form hx-post="/module/tools_library/upload" hx-target="#tl-list" hx-swap="innerHTML" hx-encoding="multipart/form-data" style="display:flex;flex-direction:column;gap:.5rem;margin-top:.6rem">
                               <label style="font-size:.75rem;color:var(--text_muted)">Folder name (used in URLs - letters, numbers, - and _ only)<input type="text" name="name" class="module-select" required></label>
                               <label style="font-size:.75rem;color:var(--text_muted)">Zip file (its contents become the tool's folder - include manifest.json inside)<input type="file" name="archive" accept=".zip" required></label>
                               <button type="submit" class="button">Upload</button>
                           </form>
                       </details>""" if admin else ""
    return (rows or "<p>None published yet.</p>") + upload_html

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return HTMLResponse(f'<div style="max-width:40rem;margin:0 auto;padding:1.5rem"><h3>Internal Tool Downloads</h3><div id="tl-list">{_body_html(_is_admin(request))}</div></div>')

@router.get("/open/{name}/{path:path}")
async def open_asset(name: str, path: str):
    p = DIST_DIR / name / path
    if not p.is_file(): raise HTTPException(404)
    return FileResponse(p)

@router.get("/download/{name}")
async def download(name: str):
    folder = DIST_DIR / name
    if not folder.is_dir(): raise HTTPException(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folder.rglob("*"):
            if f.is_file(): zf.write(f, f.relative_to(folder))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

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

@router.post("/delete/{name}", response_class=HTMLResponse)
async def delete_tool(name: str, request: Request):
    if not _is_admin(request): return HTMLResponse("Unauthorized", status_code=403)
    shutil.rmtree(DIST_DIR / name, ignore_errors=True)
    return HTMLResponse(_body_html(True))