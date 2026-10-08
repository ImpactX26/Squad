"""Dependencies shared by the routers."""

from starlette.requests import HTTPConnection

from app.core.config import Settings


def get_app_settings(conn: HTTPConnection) -> Settings:
    """The settings create_app() was built with (HTTP and WebSocket routes alike)."""
    return conn.app.state.settings
