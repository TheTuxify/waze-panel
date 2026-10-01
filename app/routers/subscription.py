from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import VpnUser
from app.openvpn import certs
from app.openvpn.templates import build_ovpn
from app.pwa import web_manifest
from app.templating import templates
from app.xray import links as xray_links

router = APIRouter()


def _get_user_or_404(token: str, db: Session) -> VpnUser:
    user = db.query(VpnUser).filter(VpnUser.token == token).first()
    if not user:
        raise HTTPException(status_code=404, detail="لینک نامعتبر است.")
    return user


def _is_browser_request(request: Request) -> bool:
    fmt = request.query_params.get("format", "").lower()
    if fmt in ("xray", "clash", "b64", "base64", "raw"):
        return False
    if fmt in ("web", "html"):
        return True

    ua = request.headers.get("user-agent", "").lower()
    accept = request.headers.get("accept", "").lower()

    # Proxy clients / apps, mobile VMs, and command-line download tools never get HTML
    client_signatures = (
        "v2ray", "v2fly", "xray", "clash", "stash", "meta", "mihomo", "flclash",
        "sing-box", "sfa", "sfm", "nekobox", "matsuri", "shadowrocket",
        "quantumult", "surge", "loon", "streisand", "v2box", "foxray",
        "surfboard", "okhttp", "dalvik", "go-http-client", "dart",
        "curl", "wget", "python-requests", "httpclient", "postman", "cfnetwork",
    )
    if any(sig in ua for sig in client_signatures):
        return False

    sec_dest = request.headers.get("sec-fetch-dest", "").lower()
    sec_mode = request.headers.get("sec-fetch-mode", "").lower()
    if sec_dest == "document" or sec_mode == "navigate":
        return True

    # Any client specifically asking for HTML that is not a known proxy client
    if "text/html" in accept:
        return True

    return False


def _render_subscription_html(token: str, request: Request, user: VpnUser, db: Session):
    status_label = user.status_label()
    usage_percent = None
    if user.data_limit_bytes:
        usage_percent = min(100, round((user.data_used_bytes / user.data_limit_bytes) * 100, 1))

    return templates.TemplateResponse(
        "subscription.html",
        {
            "request": request,
            "user": user,
            "status_label": status_label,
            "usage_percent": usage_percent,
            "panel_title": settings.PANEL_TITLE,
            "days_left": user.days_left(),
            "auth_mode": user.auth_mode or "cert",
            "openvpn": bool(user.openvpn_enabled),
            "xray_links": xray_links.user_links(db, user) if user.xray_enabled else [],
            "xray_sub": f"{settings.public_base_url}/sub/{token}/xray",
            "clash_sub": f"{settings.public_base_url}/sub/{token}/clash",
        },
    )


@router.get("/sub/{token}")
def subscription_page(token: str, request: Request, db: Session = Depends(get_db)):
    user = _get_user_or_404(token, db)

    if _is_browser_request(request):
        return _render_subscription_html(token, request, user, db)

    ua = request.headers.get("user-agent", "").lower()
    is_clash = (
        any(k in ua for k in ("clash", "stash", "meta", "mihomo", "flclash"))
        or request.query_params.get("format") == "clash"
    )
    if is_clash and user.xray_enabled:
        body, headers = xray_links.clash_subscription(db, user)
        return Response(content=body, media_type="text/yaml; charset=utf-8", headers=headers)

    if user.xray_enabled:
        body, headers = xray_links.subscription(db, user)
        return PlainTextResponse(body, headers=headers)

    if user.openvpn_enabled:
        return subscription_download(token, "udp", db)

    return _render_subscription_html(token, request, user, db)


@router.get("/sub/{token}/web")
def subscription_web(token: str, request: Request, db: Session = Depends(get_db)):
    """Explicitly render the web dashboard for any client/browser."""
    user = _get_user_or_404(token, db)
    return _render_subscription_html(token, request, user, db)


@router.get("/sub/{token}/manifest.webmanifest")
def subscription_manifest(token: str, db: Session = Depends(get_db)):
    """The user can add their subscription page to the home screen and
    check their usage like an app."""
    user = _get_user_or_404(token, db)
    return web_manifest(f"{user.username} · {settings.PANEL_TITLE}", user.username, f"/sub/{token}", f"/sub/{token}")


@router.get("/sub/{token}/xray")
def subscription_xray(token: str, request: Request, db: Session = Depends(get_db)):
    """The Xray subscription apps import and refresh: base64 of the share
    links (or Clash YAML when requested by Clash/Stash clients), with usage
    and expiry in the headers they understand."""
    user = _get_user_or_404(token, db)
    ua = request.headers.get("user-agent", "").lower()
    is_clash = (
        any(k in ua for k in ("clash", "stash", "meta", "mihomo", "flclash"))
        or request.query_params.get("format") == "clash"
    )
    if is_clash:
        body, headers = xray_links.clash_subscription(db, user)
        return Response(content=body, media_type="text/yaml; charset=utf-8", headers=headers)

    body, headers = xray_links.subscription(db, user)
    return PlainTextResponse(body, headers=headers)


@router.get("/sub/{token}/clash")
def subscription_clash(token: str, db: Session = Depends(get_db)):
    """Direct Clash / Clash Meta (Mihomo) YAML subscription configuration."""
    user = _get_user_or_404(token, db)
    body, headers = xray_links.clash_subscription(db, user)
    return Response(content=body, media_type="text/yaml; charset=utf-8", headers=headers)


@router.get("/sub/{token}/{proto}")
def subscription_download(token: str, proto: str, db: Session = Depends(get_db)):
    if proto not in ("udp", "tcp"):
        raise HTTPException(status_code=400, detail="invalid proto")
    user = _get_user_or_404(token, db)
    if not user.openvpn_enabled:
        raise HTTPException(status_code=404, detail="OpenVPN برای این اشتراک فعال نیست.")

    try:
        content = build_ovpn(db, user, proto)
    except certs.CertError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    filename = f"{user.username}-{proto}.ovpn"
    return PlainTextResponse(
        content,
        media_type="application/x-openvpn-profile",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
