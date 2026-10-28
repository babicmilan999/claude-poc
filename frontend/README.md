# Front-ends

One Angular workspace (Angular 21, standalone components, signals, zoneless) with two apps
and a shared library, all coded against `contracts/agent-api.yaml`.

| Project | What | Dev server |
|---|---|---|
| `projects/customer-app` | Phone-sized customer app: log in as, account view, chat with streamed answers, sources and 👍 / 👎, watch mode | `npm run start:customer` (:4300) |
| `projects/ops-console` | Case queue with Simulate buttons, case detail (transcript, timeline, diagnosis, review, Re-investigate), conversations, new case form | `npm run start:ops` (:4200) |
| `projects/api-client` | Shared library `@poc/api-client`: contract types, fetch-based SSE client, API groups per audience, chat and timeline state, and the mock agent service | (source only, no build) |

## Commands

```bash
npm ci
npm test                  # library tests (Node), then both apps (jsdom); headless, runs once
npm run build             # builds both apps against the mock (default) into dist/
npm run build:real        # builds both apps against the real agent service
npm run start:customer    # dev server :4300, mock
npm run start:customer:real   # dev server :4300, real agent service at http://localhost:8000
npm run start:ops / start:ops:real   # same for the console on :4200
```

`make test-frontend` from the repo root runs `npm test`. Tests always use the mock (or a fake
`fetch`); they never need a backend.

## API: mock or real

The switch is the Angular build configuration; each app has two environment files:

| Configuration | File | `api.mode` | Used by |
|---|---|---|---|
| default (`production`, `development`) | `src/environments/environment.ts` | `mock` | `npm run start:customer` / `start:ops`, `npm run build`, every test |
| `real` (`real-development` for `ng serve`) | `src/environments/environment.real.ts` | `http`, `baseUrl: http://localhost:8000` | `npm run build:real`, `npm run start:*:real`, the Docker image |

- `mock`: `MockAgentServer` runs in the browser and answers through a `fetch` function, SSE
  streams included, so the apps work without a backend. It is seeded with six customers, a
  resolved conversation, a handed-off case awaiting review, an e-mail case awaiting review
  and a parked case.
- `http`: the real agent service at `api.baseUrl`.

`ng build <app> --configuration real` works for one app.

The same mock backs the tests. `projects/api-client/src/lib/mock/contract.spec.ts` drives every
operation in the contract through the mock and validates each response (status, content type,
body and each SSE event) against `contracts/agent-api.yaml`, so the mock cannot drift from the
contract. The `http` path is tested against a fake `fetch`: `http-client.spec.ts` (status codes,
problem+json, SSE over chunk boundaries, reconnects) and each app's `http-mode.spec.ts` (the
app in `http` mode, with dropped streams and error responses injected).

### Run-time base URL (`config.json`)

Real builds read an optional `config.json` next to `index.html` at startup, so a built app
can point at another agent service without a rebuild:

```json
{ "apiBaseUrl": "http://localhost:8000", "customerAppUrl": "http://localhost:4300" }
```

`apiBaseUrl` (both apps), `apiMode` (`http` or `mock`) and `customerAppUrl` (ops console:
watch-mode links) are optional; a missing file, a non-JSON answer or bad fields keep the
values of `environment.real.ts`. An empty `apiBaseUrl` means same origin (a reverse proxy
in front of both). Mock builds never fetch it.

### Browser to agent service: CORS, no dev proxy

The apps call the agent service directly from the browser, cross-origin. The agent service
allows `CORS_ORIGINS` (default `http://localhost:4200,http://localhost:4300`, with every
method and header, so `X-Customer-Id` and `Last-Event-ID` pass the preflight). Serve the
apps on those origins, or add yours to `CORS_ORIGINS`. There is no `ng serve` proxy, so the
dev servers and the Docker images behave the same.

### Docker image

`Dockerfile` builds one app (`--build-arg APP=customer-app` or `ops-console`) and serves it
with nginx:

| Setting | When | Default | What |
|---|---|---|---|
| `API_MODE` (build arg) | build | `real` | `real` builds `--configuration real`; `mock` builds the in-browser mock |
| `API_BASE_URL` (env) | container start | unset (`http://localhost:8000`) | agent service URL **as the browser sees it** |
| `CUSTOMER_APP_URL` (env, ops console) | container start | unset (`http://localhost:4300`) | customer app URL for watch-mode links |
| `API_MODE` (env) | container start | unset | override the mode (`http` or `mock`) |

`docker/write-config.sh` runs from nginx's `/docker-entrypoint.d/` and writes `config.json`
from these variables; with none set it writes nothing. nginx answers `/config.json` with 404
when it is absent and never caches it. Example:

```bash
docker build --build-arg APP=ops-console -t ops-console .
docker run -p 4200:80 -e API_BASE_URL=http://localhost:8000 ops-console
```

In `infra/docker-compose.yml` the defaults already match (agent service published on
host port 8000, apps on 4200 and 4300).

## Errors from the real API

- Errors are `application/problem+json`; `ApiError` keeps `status` and the problem, and
  shows `title: detail`. Non-problem bodies still give a message (FastAPI's
  `{"detail": "Not Found"}`, a proxy's HTML page). A failed `fetch` (service down, CORS)
  becomes `NetworkError` ("Cannot reach the agent service (url)").
- **401** on a customer endpoint (the agent service does not know the stored
  `X-Customer-Id`, e.g. after a database reset): the customer app logs out and asks to log in again.
- **502** on `GET /me/overview` (banking service down): "not available right now" with Try again.
- **404/409** on send: a vanished conversation is dropped; a closed one is reloaded as closed.
- **409** on Re-investigate or Investigate: the reason is shown and the case is reloaded.
- **404** on the simulation buttons: "Simulation is turned off" (`SIMULATION_ENABLED=false`).

## Knowledge base links

`Source.url` is `KB_ARTICLE_BASE_URL` + slug in the agent service, by default
`/help/<slug>`, a path on the app's own origin. No endpoint serves articles yet, so:

- the customer app routes same-origin paths inside the app; `/help/:slug` is a placeholder
  page that names the article (absolute `http(s)` URLs open in a new tab; anything else,
  and `account_data` sources, are plain chips);
- the ops console shows source titles only, without links.

## Streaming

- **Chat** (`POST /conversations/{id}/messages`): read with `fetch` and the SSE parser in
  `sse.ts`, because `EventSource` cannot POST or send `X-Customer-Id`.
- **Investigation timeline** (`GET /investigations/{id}/events`): `resumableSse` tracks the last
  `id:` and reconnects with `Last-Event-ID` (honouring `retry:`) until the run stops; a page
  that already has events starts after them. Network errors and 502/503/504 are retried, other
  HTTP errors are not. If the server closes without a final event, `GET /investigations/{id}`
  decides between reconnecting and stopping. After five empty reconnects the console offers
  Reconnect. Finished runs load the JSON list (`after_id` for the tail).
- **Chat answers** are not resumable: if the POST stream drops mid-answer, the app stops the
  caret, shows the error and reloads `GET /conversations/{id}`.
- **Case queue** (`GET /cases/events`) and **watch mode** (`GET /conversations/{id}/events`): live
  only; reopened after a drop (the queue reloads `GET /cases`; watch mode deduplicates the replay).
