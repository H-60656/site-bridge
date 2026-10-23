"""健康检查：框架自带，所有接口项目都有。"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health():
    return {"ok": True}
