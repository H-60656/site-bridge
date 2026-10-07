from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["web"])

# novel/routes/web.py → 上两级是 novel/，阅读器页面在 novel/web/index.html
WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"


@router.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB_INDEX)
