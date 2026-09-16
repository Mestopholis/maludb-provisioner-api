"""The operator console's pages (ADR-082 slice 4), served by the admin listener only.

**A fixed map of files, not a directory mount.** Five paths, each naming one file in the
repository; nothing a request says can reach any other file, and nothing under
`frontend/` except the shared stylesheet. The pages are not on the public site: this
router is in `ADMIN_ROUTERS` and nowhere else.

The pages hold no data. Everything they show is fetched from `/admin/v1` with the staff
session cookie, and the Content-Security-Policy set in `admin_main` allows script and
style from this origin only.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse

ROOT = Path(__file__).resolve().parents[3]

FILES = {
    "": (ROOT / "admin" / "index.html", "text/html; charset=utf-8"),
    "admin.js": (ROOT / "admin" / "admin.js", "text/javascript; charset=utf-8"),
    "theme.js": (ROOT / "admin" / "theme.js", "text/javascript; charset=utf-8"),
    "assets/admin.css": (ROOT / "admin" / "admin.css", "text/css; charset=utf-8"),
    # One design system for both consoles: the customer frontend's stylesheet, by name.
    "assets/styles.css": (ROOT / "frontend" / "styles.css", "text/css; charset=utf-8"),
}

router = APIRouter(prefix="/admin", include_in_schema=False)


@router.get("")
def console_root() -> RedirectResponse:
    return RedirectResponse("/admin/", status_code=status.HTTP_308_PERMANENT_REDIRECT)


def _serve(name: str) -> FileResponse:
    path, media_type = FILES[name]
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return FileResponse(path, media_type=media_type)


@router.get("/")
def console_page() -> FileResponse:
    return _serve("")


@router.get("/admin.js")
def console_script() -> FileResponse:
    return _serve("admin.js")


@router.get("/theme.js")
def console_theme() -> FileResponse:
    return _serve("theme.js")


@router.get("/assets/admin.css")
def console_styles() -> FileResponse:
    return _serve("assets/admin.css")


@router.get("/assets/styles.css")
def shared_styles() -> FileResponse:
    return _serve("assets/styles.css")
