"""Authenticated pilot diagnostics and explicit opt-in. No schema mutation endpoints."""
import hmac
import os
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import db
from . import service

router = APIRouter()


def principal(request, mutate=False):
    # Imported lazily: main owns the existing session/account rules.
    from ..main import current_user
    token = os.getenv("SYSAI_COORDINATOR_API_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    if supplied.startswith("Bearer "):
        if len(token) < 32 or not hmac.compare_digest(supplied[7:], token):
            raise HTTPException(401, "Invalid runtime credential")
        try:
            uid = int(os.getenv("SYSAI_COORDINATOR_API_USER_ID", "0"))
        except ValueError:
            raise HTTPException(401, "Runtime user is not configured") from None
        with db.SessionLocal() as s:
            row = s.get(db.User, uid)
            if not row or row.status != "approved" or row.role != "superuser":
                raise HTTPException(403, "Runtime user's current rights are insufficient")
        return {"id": uid, "runtime": True}
    user = current_user(request)
    if not user or user["role"] != "superuser":
        raise HTTPException(403, "Sign in as an approved superuser")
    if mutate:
        expected = request.session.get("coordinator_csrf", "")
        supplied = request.headers.get("x-csrf-token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            raise HTTPException(403, "Missing or invalid CSRF token")
    return {**user, "runtime": False}


@router.get("/coordinator", response_class=HTMLResponse)
def page(request: Request):
    principal(request)
    from ..main import page as render_page
    csrf = request.session.setdefault("coordinator_csrf", secrets.token_urlsafe(32))
    return render_page(request, "coordinator.html", csrf=csrf, pilot=service.snapshot())


@router.get("/api/coordinator")
def status(request: Request):
    principal(request)
    return service.snapshot()


@router.get("/api/coordinator/tasks/{run_id}")
def task_status(run_id: str, request: Request):
    principal(request)
    return service.snapshot(run_id)


@router.post("/api/coordinator/{operation}")
async def mutate(operation: str, request: Request):
    who = principal(request, mutate=True)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise service.Hold("JSON object required")
        if operation == "config":
            if who["runtime"]:
                raise HTTPException(403, "Runtime cannot change opt-in or allowlist")
            return service.configure(data)
        if operation == "enroll":
            if type(data.get("task_id")) is not int:
                raise service.Hold("task_id must be an integer")
            return {"run_id": service.enroll(data["task_id"], who["id"])}
        if operation == "tick":
            # Synchronous provider/DB work off the ASGI event loop, same existing process.
            from starlette.concurrency import run_in_threadpool
            return await run_in_threadpool(service.tick)
        if operation == "review":
            if who["runtime"]:
                raise HTTPException(403, "Human review required")
            service.review(data.get("run_id"), data.get("decision"))
            return {"ok": True}
        raise HTTPException(404, "Unknown operation")
    except (service.Hold, ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from None
