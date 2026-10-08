"""The ServiceMesh MCP servers (ARCHITECTURE.md §5).

Every server binds to loopback, whatever its MCP_*_URL says: only the client URLs
matter, and they use 127.0.0.1 rather than localhost (§4.5).
"""

from mcp.server import MCPServer

HOST = "127.0.0.1"
PATH = "/mcp"


def serve(server: MCPServer, port: int) -> None:
    """Run one server over Streamable HTTP at http://127.0.0.1:<port>/mcp."""
    server.run("streamable-http", host=HOST, port=port, streamable_http_path=PATH)
