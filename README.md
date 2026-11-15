# home-mcp

MCP server exposing home services, built on [FastMCP](https://gofastmcp.com) 4.x.

Currently serving:

- **NIBE heat pump** via [myUplink API](https://dev.myuplink.com/) — systems, devices,
  parameter points (temperatures, compressor status, …), alarms, smart home mode, settings
- **meteo.lt** — weather forecasts from the Lithuanian Hydrometeorological Service (public, no auth)

More integrations can be added as drop-in modules in `app/services/`.

## Tools

| Tool | What it does |
|---|---|
| `nibe_list_systems` | Systems and heat pumps on your account (IDs, model, connection, alarm flag) |
| `nibe_system_details` | Raw system details: devices, zones |
| `nibe_device_info` | Model, firmware, features |
| `nibe_device_points` | Current values — filter by `search` ("hot water", "outdoor"), `parameter_ids`, `category`, `writable_only` |
| `nibe_get_alarms` | Active alarms, or history with `only_active=false` |
| `nibe_get_smart_home_mode` | Current mode (Default / Away / Vacation …) |
| `nibe_set_point` ✏️ | Change a setting. Checks writable, min/max and enum options before sending |
| `nibe_set_smart_home_mode` ✏️ | Change the smart home mode |
| `meteo_find_places` | Search meteo.lt place codes |
| `meteo_forecast` | Long-term forecast for a place code |

✏️ = needs myUplink Premium. Set `MYUPLINK_READ_ONLY=1` to remove them.

## Setup

**1. Create a myUplink app** at <https://dev.myuplink.com/apps> → *Create New Application*.
Callback URL can be anything (e.g. `https://localhost`) — this server uses the
client-credentials flow, no browser login. Copy the **Client Identifier** and **Client Secret**.

**2. Install dependencies and save credentials:**

```bash
cd ~/Documents/home-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in MYUPLINK_CLIENT_ID / MYUPLINK_CLIENT_SECRET
```

The `.env` is read from the project root (not the working directory), so it works
however the MCP client launches the server. To keep it elsewhere, set
`HOME_MCP_ENV_FILE=/path/to/.env`. Variables already set in the environment
override `.env`. meteo.lt works without any credentials.

**3. Add it to your MCP client** (e.g. Claude desktop → `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "home-mcp": {
      "command": "/Users/alius/Documents/home-mcp/.venv/bin/python",
      "args": ["-m", "app"],
      "cwd": "/Users/alius/Documents/home-mcp"
    }
  }
}
```

No secrets in the client config — they stay in `.env` (`chmod 600 .env`).

**4. Restart the client.** The tools appear under the tools (🔨) menu.

## Run modes

| Mode | Command | Use for |
|---|---|---|
| stdio (default) | `python -m app` | MCP client launches it itself |
| HTTP | `python -m app --http` → `http://127.0.0.1:8000/mcp` | testing in a browser/Inspector, or a long-running local server |

Options: `--host` / `--port` (or `HOME_MCP_HTTP_HOST` / `HOME_MCP_HTTP_PORT`, or
the `PORT` env var that Cloud Run injects). `--stateless` (or
`FASTMCP_STATELESS_HTTP=1`) runs a fresh transport per request — needed when the
server can run more than one instance.

HTTP mode has **no authentication by default** — keep it on `127.0.0.1`, or set
`HOME_MCP_AUTH_TOKEN` to require `Authorization: Bearer <token>` on every request.

## Deploy to Cloud Run

The `Dockerfile` runs `python -m app --http` on `0.0.0.0:$PORT` (8080 on Cloud Run).

```bash
gcloud run deploy home-mcp \
  --source . \
  --region europe-west1 \
  --max-instances 1 \
  --set-env-vars MYUPLINK_LANGUAGE=en-GB,HOME_MCP_AUTH_TOKEN=$(openssl rand -hex 32) \
  --set-secrets MYUPLINK_CLIENT_ID=myuplink-client-id:latest,MYUPLINK_CLIENT_SECRET=myuplink-client-secret:latest \
  --allow-unauthenticated   # needed for MCP clients; the bearer token guards access
```

Notes:

- **`--allow-unauthenticated` + `HOME_MCP_AUTH_TOKEN`**: GCP IAM auth doesn't work
  with MCP clients (they can't mint Google ID tokens), so the app enforces its own
  bearer token instead. Save the generated token — you'll need it below.
- **`--max-instances 1`**: streamable HTTP keeps session state in memory. Either
  pin to one instance, or set `FASTMCP_STATELESS_HTTP=1` to allow scaling
  (a fresh transport is created per request).
- **min-instances 0** (the default) means cold starts — the first call after idle
  re-fetches the OAuth token, adding ~1s.
- Put `MYUPLINK_CLIENT_ID` / `MYUPLINK_CLIENT_SECRET` in Secret Manager
  (`gcloud secrets create ...`) — never bake `.env` into the image.

Point your MCP client at the service URL via `mcp-remote` with the token:

```json
{
  "mcpServers": {
    "home-mcp": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "https://home-mcp-XXXX.europe-west1.run.app/mcp",
        "--header", "Authorization: Bearer YOUR_TOKEN"
      ]
    }
  }
}
```

## Try

- "What's the outdoor and hot water temperature right now?"
- "Are there any alarms on the heat pump?"
- "Show me all settings I can change for heating."
- "Set hot water comfort mode to economy."
- "Put the house in Away mode."
- "What's the forecast for Vilnius this week?"

## Test without a client

```bash
cd ~/Documents/home-mcp
.venv/bin/fastmcp list app/server.py                          # list tools
.venv/bin/fastmcp call app/server.py nibe_list_systems        # real call to myUplink
.venv/bin/fastmcp call app/server.py nibe_device_points search="hot water"
.venv/bin/fastmcp dev inspector app/server.py                 # MCP Inspector in the browser
```

## Troubleshooting

- **Token request failed (401/400)** – check client ID/secret; make sure the app was
  created with the same account that owns the heat pump.
- **403 on writes** – Premium not active on that system, or your user isn't admin on it.
- **429** – rate limited; myUplink allows a limited number of calls per minute
  (systems are cached for 5 min to help stay under it).
- Logs: `~/Library/Logs/Claude/mcp-server-home-mcp.log` (macOS),
  `%APPDATA%\Claude\logs\` (Windows).

## Adding a service

1. Create `app/services/myservice.py` with a `register(mcp, settings)` function that defines `@mcp.tool` functions.
2. Add the module path to `SERVICE_MODULES` in `app/services/__init__.py`.
3. Add any config to `Settings` in `app/config.py` and `.env.example`.

## Dev

```bash
black --check app
```
