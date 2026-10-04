"""Health and version endpoints (#547)."""

from fastapi import APIRouter

from core import version as version_info

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.get("/api/version")
def api_version():
    """#508: {version, commit, deployed_at, env}."""
    return version_info.get_version_info()
