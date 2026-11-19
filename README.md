# home-mcp

MCP server exposing home services, built on [FastMCP](https://gofastmcp.com) 4.x.

Currently serving:

- **NIBE heat pump** via [myUplink API](https://dev.myuplink.com/) — systems, devices,
  parameter points (temperatures, compressor status, …), alarms, smart home mode, settings
- **Shelly devices** via the [Shelly Cloud Control API](https://shelly-api-docs.shelly.cloud/cloud-control-api/) —
  device list, status, and switch/cover/light/group control
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
| `shelly_list_devices` | All devices on the account (id, name, model, gen, online) |
| `shelly_all_status` | Last known status of every device in one call (outputs, power, sensors) |
| `shelly_device_state` | Status/settings for 1–10 devices via the v2 API, with optional `pick` filters |
| `shelly_switch_set` ✏️ | Turn a relay/plug channel on/off, optional `toggle_after` |
| `shelly_cover_set` ✏️ | Open/close/stop or position a roller/cover, incl. slats |
| `shelly_light_set` ✏️ | Light on/off, brightness, white temperature, RGB, effects |
| `shelly_group_set` ✏️ | Same-type control of several devices in one call |
| `meteo_find_places` | Search meteo.lt place codes |
| `meteo_forecast` | Long-term forecast for a place code |

✏️ = writes to a device. `nibe_set_*` needs myUplink Premium (set
`myuplink_read_only=True` in `app/config.py` to remove them). Shelly tools
accept a device **id** or a **unique device name** — call `shelly_list_devices`
first. The Shelly Cloud API is limited to ~1 request/second; the server
enforces that itself.

## Setup

**1. Create a myUplink app** at <https://dev.myuplink.com/apps> → *Create New Application*.
Callback URL can be anything (e.g. `https://localhost`) — this server uses the
client-credentials flow, no browser login. Copy the **Client Identifier** and **Client Secret**.

**1b. (optional) Get Shelly Cloud credentials** in the Shelly app → *User settings*
→ *Authorization cloud key*. Copy the **server URI** (e.g.
`shelly-208-eu.shelly.cloud`) shown on the same page — it goes in
`SHELLY_SERVER_URL`. Skip this and the Shelly tools simply won't be registered.

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
(On GCP the token is self-issued via Secret Manager instead — see below.)

## Deploy to Cloud Run

The `Dockerfile` runs `python -m app --http` on `0.0.0.0:$PORT` (8080 on Cloud Run).

**1. Put the service credentials in Secret Manager** — they're injected into the
container as env vars:

```bash
for s in myuplink-client-id myuplink-client-secret shelly-server-url shelly-auth-key; do
  gcloud secrets create "$s" --replication-policy=automatic
done
printf '%s' "$VALUE" | gcloud secrets versions add myuplink-client-id --data-file=-
# ...repeat for each secret
```

**2. Deploy:**

```bash
gcloud run deploy home-mcp \
  --source . \
  --region europe-west1 \
  --max-instances 1 \
  --set-secrets MYUPLINK_CLIENT_ID=myuplink-client-id:latest,\
MYUPLINK_CLIENT_SECRET=myuplink-client-secret:latest,\
SHELLY_SERVER_URL=shelly-server-url:latest,\
SHELLY_AUTH_KEY=shelly-auth-key:latest \
  --allow-unauthenticated   # needed for MCP clients; the bearer token guards access
```

**3. Auth key** — the server self-issues it. On first boot it generates a random
token and stores it in a `home-mcp-auth-token` secret, which it re-reads on
every later start. Grant the Cloud Run runtime service account
`roles/secretmanager.admin` so it can create/read it, then retrieve the key:

```bash
gcloud secrets versions access latest --secret=home-mcp-auth-token
```

Rotate it by adding a new secret version — the next restart picks it up. Set
`HOME_MCP_AUTH_SECRET` to use a different secret name, or set
`HOME_MCP_AUTH_TOKEN` (e.g. via `--set-secrets`) to supply the key yourself and
skip self-issuing.

Notes:

- **`--allow-unauthenticated` + bearer token**: GCP IAM auth doesn't work
  with MCP clients (they can't mint Google ID tokens), so the app enforces its own
  token instead.
- **`--max-instances 1`**: streamable HTTP keeps session state in memory. Either
  pin to one instance, or set `FASTMCP_STATELESS_HTTP=1` to allow scaling
  (a fresh transport is created per request).
- **min-instances 0** (the default) means cold starts — the first call after idle
  re-fetches the OAuth token, adding ~1s.
- Never bake `.env` into the image — all keys arrive as env vars from Secret Manager.

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
- "Which Shelly devices do I have and are they online?"
- "Turn off the living room plug."
- "Open the kitchen blinds to 40%."
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
