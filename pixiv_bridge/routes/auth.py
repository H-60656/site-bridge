from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import get_oauth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class PasswordLoginBody(BaseModel):
    username: str
    password: str


class AuthCallbackBody(BaseModel):
    code: str
    code_verifier: str


@router.get("/status")
async def auth_status(request: Request):
    oauth = get_oauth(request)
    expires_at = oauth.access_token_expires_at
    return {
        "authenticated": oauth.is_authenticated,
        "refresh_token_set": bool(oauth.refresh_token),
        "user": oauth.user,
        "access_token_expires_at": (
            datetime.fromtimestamp(expires_at).isoformat() if expires_at else None
        ),
    }


@router.post("/login")
async def auth_login(body: PasswordLoginBody, request: Request):
    return await get_oauth(request).login_password(body.username, body.password)


@router.get("/login-url")
async def auth_login_url(request: Request):
    url, verifier = get_oauth(request).login_url()
    return {"url": url, "code_verifier": verifier}


@router.post("/callback")
async def auth_callback(body: AuthCallbackBody, request: Request):
    return await get_oauth(request).exchange_code(body.code, body.code_verifier)


@router.post("/refresh")
async def auth_refresh(request: Request):
    return await get_oauth(request).refresh()
