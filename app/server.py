"""Локальный сервер Needlework: статика + API схемы и прогресса.

Запуск: python app/server.py
  На этом компьютере:  http://127.0.0.1:<порт>
  С телефона/планшета: http://<LAN-IP>:<порт>  (адреса печатаются в консоль)

При первом запуске генерирует тестовую схему projects/demo/pattern.json.
"""
import json
import re
import socket
import sys
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
STATIC_DIR = APP_DIR / "static"
PROJECTS_DIR = ROOT / "projects"

PROJECT_RE = re.compile(r"^[\w-]{1,64}$", re.UNICODE)


def ensure_demo_pattern() -> None:
    demo = PROJECTS_DIR / "demo"
    demo.mkdir(parents=True, exist_ok=True)
    target = demo / "pattern.json"
    if not target.exists():
        sys.path.insert(0, str(APP_DIR))
        from demo_pattern import build_pattern
        target.write_text(json.dumps(build_pattern(), ensure_ascii=False), encoding="utf-8")
        print("Тестовая схема создана:", target)


def project_dir(project: str) -> Path:
    if not PROJECT_RE.match(project):
        raise HTTPException(status_code=400, detail="bad project name")
    return PROJECTS_DIR / project


app = FastAPI(title="Needlework")


@app.get("/api/projects")
def list_projects():
    out = []
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir()):
            f = d / "pattern.json"
            if d.is_dir() and f.exists():
                try:
                    p = json.loads(f.read_text(encoding="utf-8"))
                    out.append({"id": d.name, "name": p.get("name", d.name),
                                "width": p.get("width"), "height": p.get("height")})
                except Exception:
                    pass
    return out


def _fix_encoding(s):
    """Починка имени файла, пришедшего в неверной кодировке (latin-1 вместо utf-8)."""
    if not s:
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _slug(s: str) -> str:
    s = re.sub(r"[^\w-]+", "_", s, flags=re.UNICODE).strip("_")
    return (s or "project")[:60]


@app.post("/api/import/pdf")
async def import_pdf_ep(file: UploadFile = File(...), name: str = Form(None)):
    data = await file.read()
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="Это не PDF-файл")
    base = _slug(_fix_encoding(name) or _fix_encoding(Path(file.filename or "схема").stem))
    slug = base
    n = 2
    while (PROJECTS_DIR / slug).exists():
        slug = f"{base}_{n}"
        n += 1
    tmp = PROJECTS_DIR / "_uploads"
    tmp.mkdir(parents=True, exist_ok=True)
    pdf_path = tmp / f"{uuid.uuid4().hex}.pdf"
    pdf_path.write_bytes(data)
    sys.path.insert(0, str(APP_DIR))
    from pdf_import import import_pdf
    try:
        import_pdf(str(pdf_path), str(PROJECTS_DIR / slug),
                   _fix_encoding(name) or base)
    except SystemExit as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        pdf_path.unlink(missing_ok=True)
    return {"ok": True, "project": slug}


@app.get("/api/pattern")
def get_pattern(project: str = "demo") -> FileResponse:
    path = project_dir(project) / "pattern.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="pattern not found")
    return FileResponse(path, media_type="application/json")


@app.get("/api/progress")
def get_progress(project: str = "demo"):
    path = project_dir(project) / "progress.json"
    if not path.exists():
        return {"done": {}}
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/progress")
async def set_progress(req: Request, project: str = "demo"):
    body = await req.json()
    done = body.get("done", {})
    if not isinstance(done, dict):
        raise HTTPException(status_code=400, detail="done must be an object")
    path = project_dir(project) / "progress.json"
    path.write_text(json.dumps({"done": done}, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "count": len(done)}


class NoCacheStaticFiles(StaticFiles):
    """Статика с запретом кеширования: правки фронтенда сразу видны в браузере."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")


def lan_addresses(port: int) -> list[str]:
    """IPv4-адреса машины в локальной сети (для доступа с телефона)."""
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addrs.add(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # фактической отправки нет
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return [f"http://{ip}:{port}" for ip in sorted(addrs)]


def free_port(start: int) -> int:
    port = start
    while port < start + 20:
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                port += 1
    return start


if __name__ == "__main__":
    ensure_demo_pattern()
    port = free_port(8000)
    print("Needlework запущен.")
    print(f"  На этом компьютере:  http://127.0.0.1:{port}")
    for url in lan_addresses(port):
        print(f"  С телефона/планшета: {url}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
