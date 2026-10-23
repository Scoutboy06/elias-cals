# TimeEdit calendar adapter

A small Python service that fetches a TimeEdit ICS feed, applies your event transformations,
and serves a subscription feed for Google Calendar, Apple Calendar, etc. No database needed.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync
uv run uvicorn adapter:app --reload --host 127.0.0.1 --port 8000
```

Open this URL to download your adapted calendar:

```text
http://localhost:8000/adapter/timeedit?id=public.ri6579QQ1Z0ZyYQ1b15QQ7Z558203Xe5n065Zu
```

`id` is `<view>.<feed>`, without `.ics`. The tenant defaults to `chalmers`.
Other TimeEdit tenants work via `&tenant=<tenant>`:

```text
/adapter/timeedit?id=public.<feed>&tenant=<tenant>
```

Alternatively, pass a full URL (URL-encode it when constructing query strings):

```sh
curl --get 'http://localhost:8000/adapter/timeedit' \
  --data-urlencode 'url=https://cloud.timeedit.net/chalmers/web/public/ri6579QQ1Z0ZyYQ1b15QQ7Z558203Xe5n065Zu.ics'
```

Use exactly one of `id` or `url`. Full URLs must be HTTPS ICS links under
`cloud.timeedit.net/<tenant>/web/<view>/<feed>.ics`, without a query or fragment.
Arbitrary hosts, interactive timetable pages, custom-domain deployments, and redirects
are deliberately unsupported to avoid turning this into an unrestricted HTTP proxy.
Interactive pages must first be exported as an ICS subscription link in TimeEdit.

API documentation: `/docs`. Health check: `/health`.

## Customize

Edit **`transforms.py`**. The default output preserves all events and their original fields.
`--reload` picks up edits automatically locally; restart the service in production.
Transformations apply to every source feed handled by this instance.

Start with `TITLE_REPLACEMENTS` and `LOCATION_REPLACEMENTS`, or edit:

| Hook | Purpose |
| --- | --- |
| `format_title(value, event)` | Format `SUMMARY` |
| `format_description(value, event)` | Format `DESCRIPTION` |
| `format_location(value, event)` | Format `LOCATION` |
| `include_event(event)` | Return `False` to exclude an event |
| `transform_event(event)` | Change any ICS field, add alarms, or return `None` to drop it |
| `transform_calendar(calendar)` | Set calendar name and other feed metadata |

The `event` is the full `icalendar.Event` object, not a reduced wrapper. Helpers:

```python
text(event, "SUMMARY")  # Missing text becomes "".
set_text(event, "LOCATION", "Room 123")  # Preserves existing field parameters.
set_text(event, "DESCRIPTION", None)  # Removes the field.
event.decoded("DTSTART")  # datetime/date; check field exists first.
```

For example, replace `format_title` with:

```python
def format_title(value, event):
    value = replace_text(value, {"TDA123": "Algorithms", "Lecture": "📚 Lecture"})
    return " ".join(value.split())
```

For custom fields, use `event.add(...)` after removing an existing value with
`event.pop(field, None)` to avoid accidentally creating duplicate properties.
See [icalendar's documentation](https://icalendar.readthedocs.io/) for typed properties.

### Dates, recurrence, and identity

Untouched properties, timezone definitions, recurrence exceptions, and alarms are preserved.
Keep `UID` stable: random IDs create duplicate events. Do not set `DTSTAMP` to the current
time on every request. Google may ignore ICS alarms; configure notifications in Google instead.

You can edit `DTSTART`/`DTEND`, but moving recurring events requires consistent handling of
`RRULE`, `RDATE`, `EXDATE`, and `RECURRENCE-ID`. Filtering a recurring master and its exceptions
must also be done consistently (for example by UID). The adapter does not expand recurrence.

## Docker Compose + Cloudflare Tunnel

This stack runs **only the adapter**. It connects to an existing `cloudflared` container
managed in another Compose project through a shared external Docker network. No host
ports are published and no Cloudflare credentials are stored in this project.
Both containers must run on the same Docker host, which must stay running for refreshes.

### 1. Share a network with your existing tunnel

Prerequisites: Docker Engine with Compose v2, an existing working Cloudflare Tunnel,
and a domain managed by Cloudflare. The examples use a network named `cloudflared`.
If your tunnel already uses a suitable network, reuse its actual Docker network name
instead, and set `TUNNEL_NETWORK` accordingly in step 2.

To create a dedicated shared network (once, if it does not already exist):

```sh
docker network create cloudflared
```

In your **existing cloudflared Compose file**, attach the connector service to this
external network. Merge this into the existing configuration, retaining its command,
credentials, and any other networks needed by your other applications:

```yaml
services:
  cloudflared:
    # Keep your existing configuration here.
    networks:
      - default
      - tunnel

networks:
  default: {}
  tunnel:
    external: true
    name: cloudflared
```

From that other Compose project's directory, apply the change:

```sh
docker compose up -d cloudflared
```

Use your actual connector service name if it is not `cloudflared`. Recreating the
connector may briefly interrupt other applications served by that tunnel.
For a temporary attachment without editing that project, you can instead run
`docker network connect cloudflared <existing-cloudflared-container-name>`, but this
attachment will be lost when the container is recreated. The Compose change is preferred.

### 2. Configure and start

From the project directory:

```sh
cp .env.example .env
chmod 600 .env
```

Set `TUNNEL_NETWORK` in `.env` to your shared network's name (default: `cloudflared`).
Compose reads `.env` automatically. No tunnel token is needed here. If upgrading from
the earlier two-container setup, remove `CLOUDFLARE_TUNNEL_TOKEN` and `CLOUDFLARED_IMAGE`
from your old `.env`; the existing tunnel's credentials stay in its own project.

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=50 adapter
```

No local Python/uv installation is required. If an earlier version of this project
already started its own connector, `docker compose up -d --build --remove-orphans`
also removes that old project-owned connector. This does not remove containers from
your other Compose project; make sure you use a distinct Compose project name.

### 3. Configure your existing tunnel's hostname

In the Cloudflare dashboard, open your **existing tunnel** and add a published
application route / public hostname:

- Hostname: e.g. `calendar.example.com` on your Cloudflare domain.
- Service type: **HTTP**, not HTTPS.
- Service URL: **`timeedit-adapter:8000`** (equivalently `http://timeedit-adapter:8000`).
- Leave the path empty to route all paths to this service.

`timeedit-adapter` is the adapter's alias on the shared network. Do not use
`localhost:8000`: that would refer to the connector container itself. Avoid giving
another container the same alias on this network. For a locally managed tunnel,
use `service: http://timeedit-adapter:8000` in its hostname ingress rule instead,
then reload/restart your connector as appropriate.

Cloudflare provides HTTPS to clients; the container-to-container hop uses HTTP over
Docker's network. No router port forwarding is needed. Use a normal bridge network
with outbound internet access so the adapter can reach TimeEdit, not an internal-only network.

### 4. Verify and subscribe

Replace `calendar.example.com` with your actual hostname:

```sh
curl --fail 'https://calendar.example.com/health'
curl --fail 'https://calendar.example.com/adapter/timeedit?id=public.ri6579QQ1Z0ZyYQ1b15QQ7Z558203Xe5n065Zu'
```

The first returns `{"status":"ok"}`; the second returns an ICS calendar.
Subscribe using:

```text
https://calendar.example.com/adapter/timeedit?id=public.ri6579QQ1Z0ZyYQ1b15QQ7Z558203Xe5n065Zu
```

Do not put a Cloudflare Access login, service-token requirement, or interactive WAF
challenge in front of the calendar endpoint: Google cannot complete those flows.
Check Cloudflare security rules if the URL works in your browser but Google cannot
subscribe. The tunnel does not add application authentication: anyone who knows a
feed URL can fetch it. Review the privacy/rate-limit guidance below before sharing it.

### Edit, update, stop

`transforms.py` is bind-mounted read-only into the adapter. Edit it on the host, then:

```sh
docker compose restart adapter
```

No rebuild is needed for formatting changes. For changes to `adapter.py`, dependencies,
or the Dockerfile, rebuild with `docker compose up -d --build`. After changing `.env`,
use `docker compose up -d` to recreate affected containers; a restart alone does not
apply environment changes. Cached feeds are lost on adapter restarts.

To rebuild the adapter with fresh base images:

```sh
docker compose build --pull adapter
docker compose up -d
```

The Python packages are pinned by `uv.lock`. Base images use floating tags; pin the
Dockerfile image references to tags/digests for fully reproducible deployments.
Update your connector separately in its own Compose project.

To stop the adapter:

```sh
docker compose down
```

This leaves your existing connector, external Docker network, and Cloudflare routes
untouched. Remove the calendar hostname separately if you no longer want to publish it.

Troubleshooting:

- **Adapter unhealthy:** `docker compose logs --tail=50 adapter`; check for syntax
  errors in `transforms.py` and ensure the host file is readable by container UID 10001.
- **Network not found:** create the shared network or correct `TUNNEL_NETWORK`.
- **Cloudflare 502:** verify the route targets **HTTP `timeedit-adapter:8000`**, both
  containers are running, and both are attached to the same Docker network.
  `docker network inspect cloudflared` lists attached containers.
- **Tunnel disconnected:** check the connector in its own Compose project. This
  project does not start or configure the tunnel connector itself.
- **Adapter 502:** the tunnel is working, but fetching or parsing the TimeEdit feed failed.

## Subscribe from Google Calendar

1. Deploy the app on a server with a publicly accessible **HTTPS** URL.
2. In Google Calendar on the web, choose **Other calendars → + → From URL**.
3. Paste `https://your-host/adapter/timeedit?id=public.<feed>`.

This must be a URL subscription, not a one-time file import. Google cannot fetch
`localhost` and controls its refresh schedule; updates can take hours.

Production command (put behind an HTTPS reverse proxy or use a Python hosting platform):

```sh
uv run uvicorn adapter:app --host 0.0.0.0 --port 8000 --no-access-log
```

Subscription URLs expose the selected timetable to anyone who has the link. There is
no authentication in this starter. Treat IDs/URLs as private; configure your hosting
platform and reverse proxy not to log query strings. For an internet-facing deployment,
add rate limits and, if desired, a secret URL prefix or token. CORS is not required for
calendar subscriptions. Source validation prevents arbitrary upstream hosts, not abuse
of the allowed TimeEdit host. The app itself does not persist calendars to disk.

## Cache and failures

Environment variables (set in the shell or your hosting service; Compose reads `.env`
automatically, but a direct `uvicorn` launch does not):

| Variable | Default | Meaning |
| --- | --- | --- |
| `CACHE_TTL_SECONDS` | `600` | Fetch at most once per source every 10 minutes |
| `STALE_TTL_SECONDS` | `86400` | Maximum source age for fallback during upstream failures |

Cache is in memory, bounded to 128 feeds per worker. Restarting clears it. Upstream fetches
are serialized to keep the service simple; use this for personal/small-scale hosting.
Fetches time out after 20 seconds and decoded feeds are limited to 5 MiB.
Failures serve the last valid source within the stale age limit, with `X-Feed-Stale: true`.
Without a usable cache, the endpoint returns 502. Invalid custom transformations return 500,
not a successful empty feed. Each request applies your current transformations to cached
source data. Google still decides when to fetch the adapter again.

## Test

```sh
uv run pytest
uv run ruff check .
```
