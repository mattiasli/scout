# Deploying the Scout server

The Scout server runs as one service on [Railway](https://railway.com), built from
`server/Dockerfile`, with one volume for the database and photos. This guide sets it up
from scratch and ends with a token you can give to an AI client.

Everything secret goes into Railway's variables. Nothing secret belongs in the
repository, which is public.

You'll need:

- A Railway account connected to the GitHub account that owns the repository.
- About 20 minutes.

## 1. Create the service

1. In Railway, choose **New project**, then **Deploy from GitHub repo**, and pick the
   Scout repository.
2. Open the new service and go to **Settings**. Set these values there. Railway's
   config files (`railway.json` / `railway.toml`) aren't used.

| Setting | Value |
|---|---|
| Source → Root directory | `/server` |
| Source → Branch | `main` |
| Source → Wait for CI | On. Railway then deploys a commit only after the GitHub Actions checks pass. |
| Source → Watch paths (optional) | `/server/**`, so firmware-only changes don't redeploy the server |
| Build → Builder | Dockerfile (found automatically at `server/Dockerfile`) |
| Deploy → Healthcheck path | `/healthz` |
| Deploy → Restart policy | On failure |
| Deploy → Serverless | Off. The car keeps a WebSocket open all the time, so the server must not sleep. |
| Deploy → Replicas | 1. The driver lock, picture links and the car connection live in one process, and a volume can only be attached to one replica. |

## 2. Add the volume

In the service, choose **Add volume** (or right-click the service on the canvas, then
**Attach volume**) and set the mount path to `/data`.

Railway sets `RAILWAY_VOLUME_MOUNT_PATH=/data` for the service and Scout uses it as its
data directory, so you don't need to set `SCOUT_DATA_DIR`. The volume holds the SQLite
database (`scout.db`), the captures and the downloaded object-detection model.

Railway volumes are owned by root. The container starts as root, gives `/data` to the
unprivileged `scout` user and then runs the server as that user, so you don't need to
change anything here.

## 3. Get a public address

Under **Settings → Networking**, choose **Generate domain**. If Railway asks for a
port, enter `8000`. You'll get an address like `https://scout-production.up.railway.app`.

You can add your own domain there too. If you do, use it everywhere below instead of the
Railway one.

## 4. Register the GitHub OAuth app

Scout signs people in with GitHub. It asks for no scopes: it only reads the public
profile (numeric id, login, name and avatar) and throws GitHub's access token away
straight after.

1. On GitHub, go to **Settings → Developer settings → OAuth Apps → New OAuth App**.
   For a repository owned by an organisation, use the organisation's settings instead.
2. Fill in:
   - **Application name:** `Scout`
   - **Homepage URL:** your address, for example `https://scout-production.up.railway.app`
   - **Authorization callback URL:** your address followed by `/auth/github/callback`,
     for example `https://scout-production.up.railway.app/auth/github/callback`
   - Leave **Enable Device Flow** off.
3. Choose **Register application**. Copy the **Client ID**.
4. Choose **Generate a new client secret** and copy it. GitHub shows it only once.

## 5. Set the variables

In the service, open **Variables** and add these. Use the **Raw editor** to paste them
all at once.

| Variable | Value |
|---|---|
| `SCOUT_PUBLIC_URL` | Your address from step 3, with `https://` and no trailing slash |
| `SCOUT_SECRET_KEY` | A long random value (see below) |
| `SCOUT_OWNER_GITHUB_ID` | Your numeric GitHub user id (see below) |
| `GITHUB_CLIENT_ID` | The Client ID from step 4 |
| `GITHUB_CLIENT_SECRET` | The client secret from step 4 |
| `PORT` | `8000` |

Optional:

| Variable | Value |
|---|---|
| `SCOUT_BOOTSTRAP_TOKEN` | An MCP token for you that works without signing in to the website. Handy for a first test; you can remove it later. |
| `SCOUT_BOOTSTRAP_CAR_KEY` | A car key. At every start Scout makes sure car `scout-1` exists with this key. Leave it out to make car keys on the admin page instead. |

To make a random value for the secret key, the bootstrap token or the car key, run this
on your own computer and paste the output:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

To find your numeric GitHub id, open `https://api.github.com/users/<your-login>` and
copy the `id` field. Scout trusts only this number, because GitHub logins can be renamed
and then claimed by someone else.

Changing `SCOUT_SECRET_KEY` later signs everyone out and breaks capture download links
already handed out. Tokens and car keys keep working.

## 6. Deploy

Saving the variables starts a deployment. When it's done:

- `https://<your address>/healthz` shows `{"ok": true, ...}`.
- The start page opens and says the car is offline (or that no car is set up yet).

The deploy log shows `Scout is up at https://<your address>`. If the server stops at
start-up with a message about a setting, fix that variable; the message says which one.

From now on, every push to `main` that touches `server/` deploys by itself once CI has
passed. During a deploy the server is down for a few seconds; the car and AI clients
reconnect on their own.

## 7. Sign in and add the car

1. Open your address and choose **Sign in with GitHub**. Because your id matches
   `SCOUT_OWNER_GITHUB_ID`, your account is approved and an admin straight away.
2. Go to **Admin** and, under **Cars**, add a car with the id `scout-1` and a name.
   The car's key is shown once: copy it.
   (If you set `SCOUT_BOOTSTRAP_CAR_KEY`, the car is already there and that's its key.)
3. On the car's setup page, enter your WiFi network, the server address
   `wss://<your address without https://>/car/ws` and the key. The admin page shows the
   exact address to use.
4. The car connects within a few seconds and shows as online on the admin page.

Each car starts as **admins only**. Switch it to **everyone** on the car's admin page
when you want other activated users to drive it.

## 8. Get your first token

1. Go to **Account** and create a token. Give it a label such as "Laptop".
2. The page shows the token once, together with ready-made commands for Claude Code,
   Codex, Grok Bot and the grok CLI. Copy the one you need.

For example, for Claude Code:

```sh
claude mcp add --transport http scout https://<your address>/mcp --header "Authorization: Bearer <token>"
```

Ask the AI to call the `status` tool. It should say the car is online.

## Letting other people in

Anyone with a GitHub account can sign in. New accounts wait until an admin approves them
on the admin page. Approved users can drive any car that's set to **everyone**, one
driver at a time. Making someone an admin is a separate step, and the owner account can't
be suspended or demoted.

## Backups

All state lives on the volume at `/data`. Railway can take volume backups from the
volume's **Backups** tab. The database is in WAL mode, so a backup taken while the server
runs is consistent.

## If something goes wrong

| Symptom | Likely cause |
|---|---|
| The deploy never starts | Wait for CI is on and a CI check failed. Look at the commit on GitHub. |
| The healthcheck fails | The server stopped at start-up. The deploy log names the setting at fault. |
| "Sign-in isn't set up yet" | `GITHUB_CLIENT_ID` or `GITHUB_CLIENT_SECRET` is missing. |
| GitHub says the redirect URI isn't valid | The callback URL in the OAuth app doesn't match `SCOUT_PUBLIC_URL` + `/auth/github/callback` exactly. |
| MCP clients get "421 Misdirected Request" | `SCOUT_PUBLIC_URL` doesn't match the address the client uses. |
| The car shows "car key refused" | The key on the car doesn't match. Rotate the key on the admin page and enter the new one. |
| Everyone is signed out after a deploy | `SCOUT_SECRET_KEY` changed. |
