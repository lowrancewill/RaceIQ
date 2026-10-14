from fastapi import APIRouter
from fastapi import UploadFile
from fastapi import File
from fastapi import Request

from fastapi.templating import Jinja2Templates

from datetime import datetime
import shutil
import os

from database import SessionLocal

from app.models.session import TelemetrySession

from app.services.telemetry import analyze_file

router = APIRouter()

templates = Jinja2Templates(
    directory="app/templates"
)

UPLOAD_FOLDER = "uploads"

os.makedirs(
    UPLOAD_FOLDER,
    exist_ok=True
)

@router.post("/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...)
):

    filepath = f"{UPLOAD_FOLDER}/{file.filename}"

    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(
            file.file,
            buffer
        )

    stats = analyze_file(filepath)

    db = SessionLocal()

    session = TelemetrySession(
        filename=file.filename,
        upload_date=datetime.now(),
        avg_speed=stats["avg_speed"],
        max_speed=stats["max_speed"],
        consistency=stats["consistency"],
        driver_style="Unknown"
    )

    db.add(session)
    db.commit()

    db.close()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats
        }
    )

@router.get("/sessions")
def sessions(request: Request):

    db = SessionLocal()

    records = db.query(
        TelemetrySession
    ).all()

    db.close()

    return templates.TemplateResponse(
        "sessions.html",
        {
            "request": request,
            "records": records
        }
    )