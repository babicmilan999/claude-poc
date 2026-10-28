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
npm test          # library tests (Node), then both apps (jsdom); headless, runs once
npm run build     # builds both apps into dist/
```

`make test-frontend` from the repo root runs `npm test`.

## API: mock or real

Each app's `src/environments/environment.ts` sets `api.mode`:

- `mock` (default): `MockAgentServer` runs in the browser and answers through a `fetch`
  function, SSE streams included, so the apps work without a backend. It is seeded with six
  customers, a resolved conversation, a handed-off case awaiting review, an e-mail case
  awaiting review and a parked case.
- `http`: the real agent service at `api.baseUrl` (default `http://localhost:8000`).

The same mock backs the tests. `projects/api-client/src/lib/mock/contract.spec.ts` drives every
operation in the contract through the mock and validates each response (status, content type,
body and each SSE event) against `contracts/agent-api.yaml`, so the mock cannot drift from the contract.

## Streaming

- **Chat** (`POST /conversations/{id}/messages`): read with `fetch` and the SSE parser in
  `sse.ts`, because `EventSource` cannot POST or send `X-Customer-Id`.
- **Investigation timeline** (`GET /investigations/{id}/events`): `resumableSse` tracks the last
  `id:` and reconnects with `Last-Event-ID` until the run stops; finished runs load the JSON list.
- **Case queue** (`GET /cases/events`) and **watch mode** (`GET /conversations/{id}/events`): live
  only; reopened after a drop (the queue reloads `GET /cases`; watch mode deduplicates the replay).
