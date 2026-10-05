"""Health and version endpoints (#547)."""

from core import version as version_info
from core import roles
from web.roles import RoleRouter, requires

router = RoleRouter(default_role=roles.PUBLIC)  # #557: routes without their own label are public


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.get("/api/version", dependencies=requires(roles.VIEWER))
def api_version():
    """#508: {version, commit, deployed_at, env}."""
    return version_info.get_version_info()
