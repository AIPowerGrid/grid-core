# Stop / Cancel Propagation — grid → worker

**Status:** proposal (for review)
**Scope:** make a client "stop" actually abort the in-flight GPU job and bill only
what was produced. The Onyx-side half (close the upstream HTTP stream on stop) is
already deployed; this doc covers the grid + worker half.

## Problem

Today a stop only frees the client. Tracing the path:

- `openai.py:_stream_openai` relays tokens from `token_stream.subscribe_tokens(job_id)`.
  When the client (e.g. Onyx) disconnects, Starlette cancels this generator; its
  `finally` runs (dry-run observe) — **but nothing tells the worker to stop.**
- The worker keeps generating to completion. `worker_ws._handle_worker_generation`
  drains every token to `done`, then `credits.settle_job` settles the **full**
  generation regardless of whether the client stayed connected
  (`_stream_openai` docstring spells this out).

Net effect: GPU time is wasted, and when charging goes live the account is billed
for output produced *after* the user pressed stop. (Harmless now —
`GRID_CHARGING_ENABLED=0` — so this is best fixed before charging flips live.)

The two relevant coroutines run on **different connections** and only meet through
Redis:

```
[request task]      openai.py _stream_openai  ── subscribe ──┐
                                                              │  Redis pub/sub
[worker conn task]  worker_ws _handle_worker_generation ──────┘  (per-job channel)
```

So the cancel signal must travel through Redis, the same way tokens already do.

## Compatibility — why this works for any front-end

The cancel **trigger is the client closing the HTTP/SSE connection**, which is the
only cancel signal the OpenAI streaming protocol actually has. Every compatible
client already does this (JS `AbortController`, OpenAI SDK `stream.close()`, a
dropped socket, curl Ctrl-C). Starlette cancels the `StreamingResponse` generator
on `http.disconnect`, so the grid reacts automatically — **no client needs to know
`job_id`, call a special endpoint, or ship an SDK.** This matches OpenAI's own
behavior (close stream → generation stops → billed for partial).

Hard rule: **do NOT require a bespoke `POST /v1/cancel/{job_id}` endpoint.** That
would force every front-end to capture an internal id and call a non-standard route
— incompatible with stock OpenAI clients. The disconnect path is the contract; an
explicit cancel route may be added later as an *optional* power-user affordance, but
never as the required mechanism.

A new front-end only has to do what HTTP clients do by default: abort the request
when its user cancels. (Onyx needed internal plumbing only because it was *draining*
the stream instead of closing it — that was an Onyx bug, now fixed.)

### Disconnect detection must reach the origin

The trigger only works if the TCP close propagates through every proxy hop. Grid is
behind nginx/Cloudflare, so confirm SSE-friendly config end-to-end:
`proxy_buffering off;` / `X-Accel-Buffering: no` on the streaming routes. With
buffering on, the origin notices the disconnect late and the cancel is delayed (no
correctness loss — the job just runs a bit longer before aborting).

## Design

A cancel is a control message on the job's Redis channel, observed by the worker-WS
loop, which forwards a `cancel` frame to the worker; the worker aborts inference and
returns a final `done` carrying `cancelled: true` + tokens actually produced;
settlement bills only those.

### 1. Signal the cancel (request side)

In `token_stream.py`, add:

```python
async def request_cancel(job_id: str) -> None:
    """Mark a job cancelled and notify the worker-side loop. Idempotent."""
    r = get_redis()
    pipe = r.pipeline()
    pipe.set(_cancel_key(job_id), "1", ex=CANCEL_TTL)      # durable flag (survives a missed pub)
    pipe.publish(_channel(job_id), json.dumps({"cancel": True}))  # wake the loop now
    await pipe.execute()

async def is_cancelled(job_id: str) -> bool:
    return await get_redis().exists(_cancel_key(job_id)) == 1
```

Fire it from `openai.py:_stream_openai` (and the Anthropic/Responses equivalents)
when the generator is torn down *before* a natural finish:

```python
finally:
    if not observed:               # we never saw the terminal DONE → client bailed
        await token_stream.request_cancel(job_id)
        await _observe_dry(...)
```

`observed=True` only on the worker's terminal chunk, so this fires exactly on
client-abort, not on normal completion. (Belt-and-suspenders: also fire on an
explicit `asyncio.CancelledError` catch.)

### 2. Observe + forward to the worker (worker-WS side)

`_handle_worker_generation`'s receive loop (`worker_ws.py` ~879 / ~1020) already
loops `await ws.receive_json()` with a timeout. Add a cancel check each iteration —
cheap because it only runs between frames:

```python
if await token_stream.is_cancelled(job_id):
    await ws.send_json({"type": "cancel", "id": job_id})
    cancelled = True
    # keep reading briefly for the worker's final `done` (bounded wait),
    # else synthesize a cancelled-done from what we have.
    break
```

To avoid waiting up to the per-frame timeout (~0.5–1s) on an idle stream, also
subscribe to the job's control channel and `select` it against `receive_json` so a
mid-generation cancel is near-instant. (Phase 1 can ship with just the per-frame
poll; the pub/sub wake is a latency optimization.)

### 3. Worker aborts inference (worker client — separate repo)

**Reality check (grid-inference-worker today):** the worker is a *proxy* that does a
single blocking, non-streaming `await self.backend.post(<engine>/v1/chat/completions)`
(`worker.py:320`) and waits for the full completion before submitting. There is no
per-token loop and nowhere it can observe a cancel — so a `cancel` frame is ignored
until the generation finishes. **P2 is the real work, and it is in the worker, not
the engine.**

The good news: the engines abort natively on client disconnect, so the worker does
NOT need a bespoke engine abort API — it just has to drop its own HTTP call to the
engine:

- **vLLM / sglang OpenAI server:** closing the worker→engine HTTP connection makes
  the engine abort the running request and free the GPU slot immediately (this is the
  engine's own disconnect-abort, same mechanism as the front-end half). Optionally
  `AsyncLLMEngine.abort(request_id)` for in-process engines.
- **Ollama / llama.cpp:** likewise stops generating on client disconnect.

So the worker change is: handle `{"type":"cancel","id":job_id}` and make the backend
call cancellable — either
- switch to `async with client.stream(..., json={..., "stream": True})` and `break`
  on cancel (preferred — also fixes token-by-token UX, which is fake on the current
  blocking worker), or
- run the blocking `post()` as an `asyncio.Task` and `.cancel()` it on the frame.

Either path closes the httpx connection to the engine → engine aborts → GPU freed.
Then send the normal terminal frame with a cancelled marker:

```json
{"type":"done","id":"<job_id>","cancelled":true,
 "usage":{"completion_tokens":<produced>}, "full_text":"<partial>"}
```

Workers that predate this frame simply ignore `cancel` and finish normally — fully
backward compatible (grid still stops relaying to the dead client; only the
GPU-abort optimization is missed).

### 4. Settle partial (worker-WS side)

In the success/settlement block, den is **already** counted server-side from the
text grid actually received and capped at `max_length`. So a cancelled job needs
only:

- count output tokens from received text (unchanged path — naturally partial),
- set `processing_gens.cancelled = true` for the row (column already exists; it's
  written `cancelled=False` at dispatch today),
- `credits.settle_job` on the partial token count (or `release_job` if you decide
  cancels are **free** — see Decisions),
- `token_stream.publish_done(job_id, ...)` so any still-attached client closes clean,
- clear the cancel key.

No new settlement code path — it's the existing terminal commit with a partial count
and the `cancelled` flag flipped.

## Billing policy (decide before charging goes live)

| Option | Worker pay (den) | Account charge | Notes |
|---|---|---|---|
| **A. Pay-for-work** (recommended) | partial tokens produced | partial tokens | Honest: worker did the work; user used it. Symmetric. |
| B. Free cancel | 0 | 0 | User-friendly, but a free way to extract N tokens of work from workers → gameable. |
| C. Pay worker, comp user | partial | 0 | Fair to workers, platform eats the cost; simplest "feels right" but a cost leak. |

Recommend **A** for economic integrity (workers always paid for real compute;
matches the "faithful, metered" model). Cancels are bounded by how fast a human
hits stop, so partial charges are tiny.

## Failure modes

- **Cancel arrives after natural finish:** `is_cancelled` true but the loop already
  saw `done` → ignore; the cancel key TTLs out.
- **Worker ignores cancel (old worker):** grid stops relaying; worker finishes;
  settles full (today's behavior) — no regression.
- **Worker dies right after cancel:** existing strike/requeue logic applies; a
  cancelled job should **not** strike the worker (it's not a worker fault) — gate the
  strike on `not cancelled`.
- **Double fire:** `request_cancel` is idempotent (SET + publish); the loop checks a
  flag, not an edge.

## Touch list

- `grid_api/services/token_stream.py` — `request_cancel`, `is_cancelled`, keys/TTL.
- `grid_api/routers/openai.py` — fire `request_cancel` on pre-finish teardown.
  (Same for `anthropic.py`, `responses.py` for parity.)
- `grid_api/routers/worker_ws.py` — per-frame cancel check, send `cancel` frame,
  `cancelled=true` settle path, skip strike on cancel.
- **worker client repo** — handle `{"type":"cancel"}` → engine abort → cancelled `done`.

## Phasing

1. **P1 (grid-only, ships independently):** `request_cancel`/`is_cancelled`,
   per-frame poll in worker-WS, send `cancel` frame, partial settle + `cancelled`
   flag. Old workers ignore the frame → safe. Already stops grid from over-relaying.
2. **P2 (worker):** engine abort + cancelled `done`. This is what actually frees the
   GPU and stops token production.
3. **P3 (polish):** control-channel pub/sub for instant cancel; cancel surfaced in
   the per-message ⓘ + usage records.
