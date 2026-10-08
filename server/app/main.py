from __future__ import annotations

import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .auth import COOKIE, Session, drop_session, finish_login, get_session, login_url
from .config import settings
from .engine import SourceError, merge, read_source
from .template import TemplateInfo, TemplateField, TemplateSection, analyse_template

log = logging.getLogger("tmerge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _check_config() -> None:
    if settings.demo:
        return
    missing = [n for n, v in (("ENTRA_TENANT_ID", settings.entra.tenant_id), ("ENTRA_CLIENT_ID", settings.entra.client_id), ("ENTRA_CLIENT_SECRET", settings.entra.client_secret)) if not v]
    if missing:
        print(f"Missing required settings: {', '.join(missing)}. Fill them in .env, or set DEMO_MODE=true to try the app with sample documents.", file=sys.stderr)
        sys.exit(1)


_check_config()
app = FastAPI(title="Template Merge", docs_url=None, redoc_url=None, openapi_url=None)
pool = ThreadPoolExecutor(max_workers=3)
DEMO_USER = {"id": "demo", "upn": "demo.user@example-corp.com", "name": "Demo User"}


@app.middleware("http")
async def headers_and_session(request: Request, call_next):
    s, new = get_session(request.cookies.get(COOKIE))
    request.state.session = s
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD") and request.headers.get("x-tm") != "1":
        return JSONResponse({"error": "Request blocked."}, status_code=403)
    response: Response = await call_next(request)
    if new:
        response.set_cookie(COOKIE, s.id, httponly=True, samesite="lax", secure=settings.base_url.startswith("https://"), max_age=settings.session_hours * 3600)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
    return response


@app.exception_handler(HTTPException)
async def http_error(_req: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def session(request: Request) -> Session:
    return request.state.session


def user(s: Session = Depends(session)) -> dict:
    if not s.user:
        raise HTTPException(401, "Sign in to continue.")
    return s.user


# ---------- sign-in ----------

@app.get("/auth/login")
def auth_login(s: Session = Depends(session)):
    if settings.demo:
        s.user = DEMO_USER
        return RedirectResponse("/", 302)
    return RedirectResponse(login_url(s), 302)


@app.get("/auth/callback")
def auth_callback(request: Request, s: Session = Depends(session)):
    try:
        s.user = finish_login(s, dict(request.query_params))
        log.info("sign-in %s", s.user["upn"])
        return RedirectResponse("/", 302)
    except Exception as e:
        return RedirectResponse("/?signin_error=" + quote(str(e)), 302)


@app.get("/api/me")
def me(s: Session = Depends(session)):
    return {
        "user": s.user,
        "demo": settings.demo,
        "orgName": settings.org_name,
        "providers": settings.ai_providers(),
        "defaultProvider": settings.default_ai if any(p["key"] == settings.default_ai for p in settings.ai_providers()) else (settings.ai_providers()[0]["key"] if settings.ai_providers() else None),
        "confluence": settings.demo or settings.confluence.configured,
        "maxUploadMb": settings.max_upload_mb,
    }


@app.post("/api/logout")
def logout(response: Response, s: Session = Depends(session)):
    drop_session(s.id)
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------- templates ----------

async def _read_upload(f: UploadFile) -> bytes:
    data = await f.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"{f.filename} is larger than {settings.max_upload_mb} MB.")
    return data


@app.get("/api/templates")
def templates(_u=Depends(user)):
    return db.list_templates()


@app.post("/api/templates")
async def upload_template(file: UploadFile = File(...), name: str = Form(""), u=Depends(user)):
    fname = file.filename or "template.docx"
    if not fname.lower().endswith(".docx"):
        raise HTTPException(400, "Templates must be Word .docx files. Save older .doc or .dotx files as .docx first.")
    data = await _read_upload(file)
    try:
        info = analyse_template(data)
    except Exception as e:
        raise HTTPException(400, f"Could not read {fname} as a Word document: {e}")
    if not info.sections:
        raise HTTPException(400, info.notes[0] if info.notes else "No sections found in the template.")
    tid = db.add_template((name or re.sub(r"\.docx$", "", fname, flags=re.I)).strip()[:120], fname, u["upn"], data, info.to_json())
    log.info("template %s uploaded by %s", tid, u["upn"])
    return db.get_template(tid)


@app.get("/api/templates/{tid}")
def template(tid: str, _u=Depends(user)):
    t = db.get_template(tid)
    if not t:
        raise HTTPException(404, "Template not found.")
    return t


@app.get("/api/templates/{tid}/file")
def template_file(tid: str, _u=Depends(user)):
    t = db.get_template(tid, with_blob=True)
    if not t:
        raise HTTPException(404, "Template not found.")
    return Response(t["blob"], media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(t['filename'])}"})


@app.delete("/api/templates/{tid}")
def remove_template(tid: str, u=Depends(user)):
    db.delete_template(tid)
    log.info("template %s deleted by %s", tid, u["upn"])
    return {"ok": True}


# ---------- merges ----------

def _info_from_json(j: dict) -> TemplateInfo:
    return TemplateInfo(
        title_placeholder=j["title_placeholder"],
        sections=[TemplateSection(**s) for s in j["sections"]],
        fields=[TemplateField(**f) for f in j["fields"]],
        notes=j.get("notes", []),
    )


def _run(jid: str, template_id: str, source_name: str, kind: str, blob: bytes | None, ref: str | None, provider: str | None) -> None:
    try:
        t = db.get_template(template_id, with_blob=True)
        if not t:
            raise SourceError("The template was deleted before this merge ran.")
        doc = read_source(kind, source_name, blob, ref)
        res = merge(t["blob"], _info_from_json(t["info"]), doc, source_name, provider)
        db.finish_job(jid, title=res.source_title, report=res.report, preview=res.preview, output=res.output)
        log.info("merge %s complete (%s -> %s)", jid, source_name, t["name"])
    except Exception as e:
        log.exception("merge %s failed", jid) if not isinstance(e, SourceError) else log.info("merge %s failed: %s", jid, e)
        db.finish_job(jid, error=str(e))


def _start(t: dict, name: str, kind: str, blob: bytes | None, ref: str | None, provider: str | None, u: dict) -> str:
    jid = db.add_job(t, name, kind, ref, blob, provider, u["upn"])
    pool.submit(_run, jid, t["id"], name, kind, blob, ref, provider)
    return jid


def _provider(matching: str, provider: str) -> str | None:
    if matching != "ai":
        return None
    keys = [p["key"] for p in settings.ai_providers()]
    if provider not in keys:
        raise HTTPException(400, "That AI provider is not set up on the server.")
    return provider


@app.post("/api/jobs")
async def start_jobs(
    template_id: str = Form(...),
    matching: str = Form("headings"),
    provider: str = Form(""),
    confluence: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    u=Depends(user),
):
    t = db.get_template(template_id)
    if not t:
        raise HTTPException(404, "Template not found.")
    prov = _provider(matching, provider)
    refs = [r.strip() for r in re.split(r"[\n,]+", confluence) if r.strip()]
    if refs and not (settings.demo or settings.confluence.configured):
        raise HTTPException(400, "Confluence is not set up on the server.")
    if not files and not refs:
        raise HTTPException(400, "Add at least one document or Confluence page.")
    if len(files) + len(refs) > 50:
        raise HTTPException(400, "Merge up to 50 documents at a time.")
    started = []
    for f in files:
        name = f.filename or "document"
        if not re.search(r"\.(docx|pdf)$", name, re.I):
            raise HTTPException(400, f"{name} is not a Word (.docx) or PDF file.")
        started.append(_start(t, name, "file", await _read_upload(f), None, prov, u))
    for ref in refs:
        started.append(_start(t, ref, "confluence", None, ref, prov, u))
    return {"started": started}


@app.post("/api/demo/samples")
def demo_samples(template_id: str = Form(...), matching: str = Form("headings"), provider: str = Form(""), u=Depends(user)):
    if not settings.demo:
        raise HTTPException(404, "Not found.")
    from . import demo

    t = db.get_template(template_id)
    if not t:
        raise HTTPException(404, "Template not found.")
    prov = _provider(matching, provider)
    return {"started": [
        _start(t, "access-management-policy.docx", "file", demo.sample_source_docx(), None, prov, u),
        _start(t, "remote-work-standard.pdf", "file", demo.sample_pdf(), None, prov, u),
        _start(t, "https://example-corp.atlassian.net/wiki/spaces/IT/pages/1001/Acceptable+Use+Policy", "confluence", None, "demo", prov, u),
    ]}


@app.get("/api/jobs")
def jobs(_u=Depends(user)):
    return db.list_jobs()


@app.get("/api/jobs/{jid}")
def job(jid: str, _u=Depends(user)):
    j = db.get_job(jid)
    if not j:
        raise HTTPException(404, "Merge not found.")
    return j


@app.post("/api/jobs/{jid}/rerun")
def rerun(jid: str, matching: str = Form("headings"), provider: str = Form(""), template_id: str = Form(""), u=Depends(user)):
    src = db.get_job_source(jid)
    if not src:
        raise HTTPException(404, "Merge not found.")
    t = db.get_template(template_id or src["template_id"])
    if not t:
        raise HTTPException(404, "Template not found.")
    return {"started": [_start(t, src["source_name"], src["source_kind"], src["source_blob"], src["source_ref"], _provider(matching, provider), u)]}


@app.get("/api/jobs/{jid}/download")
def download(jid: str, _u=Depends(user)):
    got = db.get_job_output(jid)
    j = db.get_job(jid)
    if not got or not j:
        raise HTTPException(404, "No merged document for this merge.")
    title, data = got
    base = re.sub(r"[^\w\- ]+", "", title).strip()[:80] or "merged"
    fname = f"{base} ({j['template_name']}).docx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


@app.delete("/api/jobs/{jid}")
def remove_job(jid: str, _u=Depends(user)):
    db.delete_job(jid)
    return {"ok": True}


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
def api_404(rest: str):
    raise HTTPException(404, "Not found.")


# ---------- demo seed ----------

if settings.demo and not db.list_templates():
    from . import demo

    data = demo.sample_template()
    db.add_template("Information Security Policy (sample)", "infosec-policy-template.docx", DEMO_USER["upn"], data, analyse_template(data).to_json())


# ---------- web app ----------

web = Path(settings.web_dir).resolve()
if web.exists():
    app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")

    @app.get("/{rest:path}")
    def spa(rest: str):
        return FileResponse(web / "index.html", headers={"Cache-Control": "no-store"})
