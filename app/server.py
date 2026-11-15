import argparse
import logging
import os
import sys

from fastmcp import FastMCP

from app.config import get_settings
from app.services import register_all

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Optional bearer-token auth for HTTP transport. If HOME_MCP_AUTH_TOKEN is set,
# clients must send "Authorization: Bearer <token>". Stdio ignores auth.
_auth = None
if _token := os.environ.get("HOME_MCP_AUTH_TOKEN"):
    from fastmcp.server.auth import StaticTokenVerifier

    _auth = StaticTokenVerifier(
        tokens={_token: {"client_id": "home-mcp", "scopes": []}}
    )

mcp = FastMCP(
    name="home-mcp",
    instructions=(
        "Home services: NIBE heat pump via myUplink (status, temperatures, points, "
        "alarms, settings) and weather forecasts from meteo.lt (Lithuanian "
        "Hydrometeorological Service). Call nibe_list_systems first to get IDs. "
        "Before nibe_set_point or nibe_set_smart_home_mode, show the user the "
        "current value and confirm the change."
    ),
    auth=_auth,
)

register_all(mcp, get_settings())


def main() -> None:
    parser = argparse.ArgumentParser(description="home-mcp server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve over Streamable HTTP at http://HOST:PORT/mcp instead of stdio",
    )
    parser.add_argument(
        "--host", default=os.environ.get("HOME_MCP_HTTP_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(
            os.environ.get("HOME_MCP_HTTP_PORT") or os.environ.get("PORT", "8000")
        ),
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="stateless HTTP — new transport per request, safe behind "
        "autoscalers (or set FASTMCP_STATELESS_HTTP=1)",
    )
    args = parser.parse_args()

    if args.http:
        if args.host not in ("127.0.0.1", "localhost", "::1") and _auth is None:
            print(
                f"warning: listening on {args.host} with no authentication — "
                "anyone who can reach this port can control your heat pump. "
                "Set HOME_MCP_AUTH_TOKEN to require a bearer token.",
                file=sys.stderr,
            )
        kwargs = {}
        if args.stateless or os.environ.get("FASTMCP_STATELESS_HTTP", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            kwargs["stateless_http"] = True
        mcp.run(transport="http", host=args.host, port=args.port, **kwargs)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
