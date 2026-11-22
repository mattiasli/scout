# Scout server

One Python program that serves the Scout website, the MCP endpoint for AI clients and
the WebSocket the car connects to. See the [main README](../README.md) for the whole
project and [docs/deploy.md](../docs/deploy.md) for putting it on Railway.

| Path | What |
|---|---|
| `/` | Start page: what Scout is and whether the car is online |
| `/install` | How to connect Claude Code, Codex, Grok Bot and the grok CLI |
| `/login`, `/auth/github/callback`, `/logout` | Sign in with GitHub |
| `/account` | Account status, personal MCP tokens, your captures |
| `/admin`, `/admin/cars/<id>`, `/admin/activity` | Users, cars and the activity log (admins only) |
| `/mcp` | MCP over Streamable HTTP, token in `Authorization: Bearer <token>` |
| `/mcp/<token>` | The same, with the token in the path, for clients that only take a URL |
| `/car/ws` | The car's WebSocket ([protocol](../protocol/README.md)) |
| `/i/<id>` | Short-lived picture links from tool results (about 15 minutes) |
| `/captures/<id>.zip` | Capture downloads |
| `/healthz` | Health check (JSON) |

## Run it locally

You need [uv](https://docs.astral.sh/uv/). From this folder:

```sh
uv sync
SCOUT_DEV=1 uv run scout-server
```

On Windows PowerShell:

```powershell
uv sync
$env:SCOUT_DEV = "1"; uv run scout-server
```

Then open <http://127.0.0.1:8000>. Development mode allows plain `http://` and `ws://`,
uses non-secure cookies and makes up a session key if `SCOUT_SECRET_KEY` isn't set, so
sign-ins end when the server restarts. Data goes to `./data`.

To use the MCP endpoint without setting up GitHub sign-in, give yourself a bootstrap
token and a car key:

```sh
export SCOUT_DEV=1
export SCOUT_OWNER_GITHUB_ID=<your numeric GitHub id>
export SCOUT_BOOTSTRAP_TOKEN=$(python -c "import secrets; print('scout_' + secrets.token_urlsafe(32))")
export SCOUT_BOOTSTRAP_CAR_KEY=$(python -c "import secrets; print('scoutcar_' + secrets.token_urlsafe(32))")
uv run scout-server
```

Your numeric GitHub id is the `id` field at `https://api.github.com/users/<your-login>`.

### The simulated car

In a second terminal, start the simulator with the car key (the value of
`SCOUT_BOOTSTRAP_CAR_KEY`, or a key made on the admin page):

```sh
uv run scout-sim --url ws://127.0.0.1:8000/car/ws --key <key>
```

Then point an MCP client at the server, for example:

```sh
claude mcp add --transport http scout http://127.0.0.1:8000/mcp --header "Authorization: Bearer <token>"
```

### GitHub sign-in locally

Register a second GitHub OAuth app just for development (the steps are in
[docs/deploy.md](../docs/deploy.md)) with the homepage `http://127.0.0.1:8000` and the
callback `http://127.0.0.1:8000/auth/github/callback`. Then set `GITHUB_CLIENT_ID` and
`GITHUB_CLIENT_SECRET` before starting the server.

## Settings

All settings are environment variables. Nothing secret belongs in the repository.

| Variable | Meaning |
|---|---|
| `PORT` | Port to listen on. Default `8000`. |
| `SCOUT_PUBLIC_URL` | The public address, such as `https://scout-production.up.railway.app`, with no path. Used for links, the GitHub callback and the host names the MCP endpoint accepts. Falls back to `https://$RAILWAY_PUBLIC_DOMAIN`. Required unless `SCOUT_DEV=1`. |
| `SCOUT_SECRET_KEY` | Signs sessions and download links. At least 32 characters. Required unless `SCOUT_DEV=1`. Changing it signs everyone out. |
| `SCOUT_DATA_DIR` | Where the database and captures live. Default: `$RAILWAY_VOLUME_MOUNT_PATH`, else `./data`. |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | The GitHub OAuth app. Without them the sign-in page says sign-in isn't set up yet. |
| `SCOUT_OWNER_GITHUB_ID` | The owner's numeric GitHub user id. That account is always approved and an admin, and can't be suspended or demoted. Only the id counts, because logins can be renamed and reused. |
| `SCOUT_BOOTSTRAP_TOKEN` | Optional. An MCP token for the owner, so MCP works before GitHub sign-in is set up. At least 32 characters; needs `SCOUT_OWNER_GITHUB_ID`. |
| `SCOUT_BOOTSTRAP_CAR_KEY` | Optional. Makes sure a car `scout-1` exists with this key. While it's set, it replaces any key made on the admin page at every restart. |
| `SCOUT_DEV` | `1` for local development: plain `http://` and `ws://`, non-secure cookies. |
| `SCOUT_RUN_AS` | Set by the Dockerfile. When the server starts as root, it hands the data directory to this user and switches to it before opening anything. |

## How it fits together

- `scout.app.create_app` builds the Starlette app. It creates the robot side
  (`scout.robot.Robot`) and starts and stops it with the app; tests pass in a fake robot
  instead. The app side only talks to the robot through `scout.contracts.RobotAPI`.
- `scout.mcp_endpoint` checks the token, then hands the request to the MCP SDK's
  Streamable HTTP session manager in stateless mode, so a restart never strands a
  client. `scout.tools` holds the tool descriptions and input schemas, and
  `scout.service` applies the rules on every call: account status, the car's
  availability, the driver lock, then the robot call and the activity log.
- `scout.db` is SQLite in WAL mode with versioned migrations (`PRAGMA user_version`).
  The queries are small indexed lookups, so they run directly on the event loop.
- `scout.web` is the website: Jinja templates, one stylesheet and no JavaScript.
- The server never logs tokens. Uvicorn's access log shows `/mcp/<redacted>` for path
  tokens.

## Tests and checks

From this folder:

```sh
uv run pytest tests/app        # the app side
uv run pytest                  # everything
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The app tests use a fake robot and a fake GitHub, so they need no network and no car.
`tests/app/test_e2e.py` runs the real robot, the simulator over a real WebSocket and an
MCP client together. It skips itself until the robot side and the simulator are both
there.

## Container image

`Dockerfile` builds the image Railway runs (`python:3.12-slim` with
`uv sync --frozen --no-dev`). Docker isn't needed for development; CI builds the image
on every push. The image runs the server as the unprivileged user `scout`. Railway's
volumes are owned by root, so the container starts as root, and `scout-server` gives the
data directory to `scout` and drops to that user before it opens the database or the
port.
