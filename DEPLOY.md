# Deploying TheList

Port **8020**, `thelist.borant.eu`, `/opt/apps/thelist`. Docker behind Caddy,
like the rest of the house.

## First deploy

```bash
git clone https://github.com/that-ugly-cat/thelist.git /opt/apps/thelist
cd /opt/apps/thelist
cp .env.example .env
```

Fill in `.env`:

- `JWT_SECRET` — `openssl rand -hex 32`. The app refuses to start without it, on
  purpose: a default secret is worse than no app.
- `PUBLIC_URL=https://thelist.borant.eu` — **not optional**. The MCP transport
  validates the `Host` header against DNS rebinding and refuses every name it
  does not know. Leave it out and the MCP surface looks broken with nothing
  saying why — and you cannot reproduce it from the box, because on `127.0.0.1`
  the same call works.
- `AUTH_MODE` — start on `local`, switch to `gateway` once the Caddy block is in.
- SMTP — optional. With it off the app behaves identically and invitation links
  are shown on screen.

```bash
docker compose up -d --build
docker exec thelist python seed.py     # the first account, local mode
curl -s localhost:8020/health          # {"ok":true}
```

## Caddy

Generate the block rather than writing it — it is derived from the public paths
declared in `main.py`, so a new public route cannot be forgotten:

```bash
docker exec thelist python caddy.py
```

It produces the house form: `/mcp /mcp/*` outside the gate (a machine surface
cannot handle a redirect to a login page), the public matcher with `noforge` and
`nocookie`, and everything else behind `borantid`.

Reload Caddy the usual way. The reverse-proxy work needs root; the key that can
do it is noted in the wiki, not here.

## Turning the gate on

1. Put the Caddy block in and reload.
2. **Register the app in Borant ID** — this is the step that is easy to miss,
   because nothing fails loudly without it: the gate will still redirect, but
   there is no `App` row to hang a grant on, so nobody can be let in. Add a line
   to `PERIMETER` in `borant-id/seed.py`, deploy that, and run
   `docker exec borantid python seed.py --apps` (idempotent — it only adds what
   is missing). TheList declares **no roles**: the role here is per-board and
   lives in `memberships`, and the app reads no hint header.
3. Grant access to the people who need it, from Borant ID's `/admin`. **A new
   account arrives with zero grants**: without one, a legitimate person meets a
   closed door and it looks like a bug.
4. Set `AUTH_MODE=gateway` and `BORANT_TRUSTED_PROXY` in `.env`.
5. `docker compose up -d`.

**`BORANT_TRUSTED_PROXY` is the bridge gateway, not `127.0.0.1`.** Under Docker
the proxy connects from the bridge, so read the real value off the running
container rather than assuming:

```bash
docker inspect thelist --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
```

Two things learned doing this for real on 27 Aug 2026, both of which make the
obvious command the wrong one:

- **`ip route` inside the container does not work**: `python:3.12-slim` has no
  `iproute2`, and the failure is silent — an empty answer that looks like an
  answer. `docker inspect` asks the daemon instead of the container and cannot
  fail that way.
- **The value is per-app, not per-host.** Every compose project gets its own
  network: the other apps on this box sit on `172.21.0.1`, `192.168.48.1`,
  `192.168.96.1` and so on, and TheList landed on `192.168.192.1`. Copying a
  neighbour's value gives an app that refuses every identity header and answers
  503 with a message about the gate not being in front of it.

Corollary worth knowing before it bites: recreating the compose network (a
`docker compose down` followed by `up`, not a plain `up -d`) can hand the
project a different subnet, and the stale value in `.env` then rejects
everything. If the app starts answering 503 after a restart, read the gateway
again.

In gateway mode identity headers are believed **only** from that address. They
are ignored everywhere else, with a warning in the log.

### Checking the gate is really there

The number is not the proof. With `/mcp` correctly outside the gate, a call
without a key returns 401 from the *app* — body `{"error":"missing or invalid
API key"}`. If `/mcp` were accidentally inside the gate it would also return 401,
but from Borant ID, with a different body, and a `text/html` request would become
a 302 to the gate instead. **Two refusals with the same number and opposite
causes.** Read the body:

```bash
curl -s -X POST https://thelist.borant.eu/mcp \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# expected: {"error":"missing or invalid API key"}

curl -s -o /dev/null -w '%{http_code}\n' https://thelist.borant.eu/app
# expected: 302 to the gate — proof the gate did not get switched off
```

Test the advertised endpoint **without** the trailing slash: that is the form
clients use, a Starlette mount answers it with a 307, and MCP clients do not
follow redirects on POST. The middleware normalises it — but the bug is invisible
if you only ever test with the slash, which is how the person who just wrote it
tests.

### Linking existing local accounts

An account created in `local` mode and the same person arriving through the gate
are linked automatically **when the addresses match**, once, with a line in the
log. The address comes from the gate and not from the client, so this is safe —
but check the log after the first gated login of each person, because a wrong
link is only visible there.

## Backups

Everything is in `data/thelist.db`. Stop nothing, just copy:

```bash
docker exec thelist sh -c "sqlite3 data/thelist.db '.backup /tmp/b.db'" \
  && docker cp thelist:/tmp/b.db ./thelist-$(date +%F).db
```

## Upgrades

```bash
cd /opt/apps/thelist && git pull && docker compose up -d --build
```

Schema changes are additive and applied at startup: `_MIGRATIONS` in `models.py`
runs one `ALTER TABLE` per new column and ignores the duplicate errors. Nothing
destructive ever runs automatically.
