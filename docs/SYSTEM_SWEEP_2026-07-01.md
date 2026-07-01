# AIPG full-system critical sweep — 2026-07-01

Eight parallel read-only audits: grid-core, both workers, aipg-chat fork, gallery+console,
website+docs, all AGENTS.md, SDKs/bots, and a premise/architecture review. Nothing was
modified. Findings deduplicated and prioritized below.

---

## 0. URGENT — decide now

**LIVE secrets committed to a PUBLIC repo.** `aipg-art-gallery/docs/SECURITY_AUDIT_REPORT.md`
(on `origin/main`, public) contains in plaintext: R2 `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`
(aipgcoregen / media.aipg.art), `POSTGRES_CONN_STR` (gallery DB, with password), and
`AIPG_API_KEY=CADW…` (the gallery's paid-flag service grid key that bills generations).
Deleting the file does NOT un-leak — **rotate all four**, then `git rm` + history-scrub + force-push.
Blast radius if abused: media storage takeover, gallery DB access, billed grid usage on the house key.

---

## 1. Headline verdict

The **internal** system is coherent and unusually well-documented; the core (grid_api) is
well-engineered (atomic `record_and_settle`, ledger-first design, reservation billing shipped dark).
Three things drag on it: (a) **the public story oversells** — "autonomous, no kill switch, TEE,
p2p" for a centrally-coordinated network with a single admin key; (b) **the retired horde `/api/v2`
is still wired into half your own clients and most of your docs**; (c) **a stale model catalog
(Flux/SDXL/SD/Llama/Mistral)** contradicts the live set across website, docs, gallery, SDKs. None
of it is fatal; it's a coherent system wearing an inaccurate coat.

---

## 2. P0 — correctness/security affecting prod today

- **Gallery live-secrets leak** — see §0.
- **grid-core worker_ws.py:501** — the 60s registry TTL is refreshed only in the idle loop, never
  during a running job. Any job >60s (long video/reasoning) drops the worker from
  `grid:workers:active` → `/v1/models` goes empty, new requests 404/503 while the worker is healthy
  and busy. Fix: refresh the status key on every received worker frame.
- **grid-core token_stream.py:255-278** — buffer-poll fallback + pub/sub can yield the same SSE event
  twice and desync counters, occasionally skipping DONE (client hangs to idle timeout). Fix: dedup by
  a monotonic sequence number stamped at publish, not two independent counters.
- **grid-media-worker control panel** — binds `0.0.0.0:7860` with **zero auth** and renders
  `GRID_API_KEY` in HTML; `/api/settings` rewrites `.env`. Anyone on the operator's LAN steals the
  grid key. Fix: port the inference worker's `DASHBOARD_TOKEN` auth guard (already exists in sibling).
- **grid-media-worker default transport** — defaults to the retired `/v2` poll loop (`GRID_WS=false`);
  a stranger installing it today earns nothing and logs generic errors. Fix: default WS, make poll
  refuse loudly.
- **grid-media-worker ws_worker.py:218** — a failed ComfyUI prompt (OOM/bad node) never produces
  outputs; `_collect_outputs` polls `/history` forever, and since job handling is awaited inline it
  wedges the whole worker (stops reading WS). Fix: check history error/completed status + hard
  deadline; move job handling off the recv loop.
- **grid-discord-image-bot** — real (dead-system) grid key committed for ~1yr. [Justin: key is dead;
  bot being scrapped → archive the repo.]

## 3. Cross-cutting themes (fix once, resolves many findings)

### A. Retire `/api/v2` from our own clients + docs — the single most widespread inaccuracy
Retired server-side, still present in CODE: aipg-art-gallery (this checkout pre-/v1-cutover),
grid-sdk-js + grid-sdk-python (`GridRaw` — the only video/img2img/LoRA path), aigarth-agent
(image gen + grid status skills → **the public README advertises broken features**),
grid-chat-new (superseded), grid-discord-image-bot (scrapping).
Presented as LIVE/DEFAULT in DOCS: both worker AGENTS.md (documented as *default transport*),
`aipg-documentation/pages/developers.mdx:51`, `streaming-api.mdx:291`.
→ Reimplement SDK `GridRaw` on `/v1/videos/generations` + extended `/v1/images` params (keep
method signatures); port aigarth's two skills to `/v1`; mark/remove the /v2 doc sections.

### B. Stale model catalog — contradicts the live set everywhere
Live: text gpt-oss-120b/20b, qwen3-27b, deepseek-v4-flash-nvfp4, Gemma4-26B_A4B-uncensored;
media Krea 2 Turbo / z-image-turbo / FLUX.2 Klein 4B FP8 / LTX-2.3.
Stale (Flux1.1/SDXL/SD/Llama/Mistral/Wan): website Products.js/about, docs generate.mdx +
grid-overview + arc20_token_network + worker-media, gallery `config/styles.json` (offers only
z-image, no video!) + `model_presets.json`, grid-image-model-reference (26 dead models), both SDK
READMEs (`grid/llama-3.3-70b-versatile`, `LTX-2`, `FLUX.1-dev`).
→ One canonical model list; generate gallery/reference from live `/v1/models`; sweep copy.

### C. SIWE is replayable — same bug in THREE auth surfaces
grid-core `accounts.py:105`, aipg-chat `wallet.py:81`, gallery `app.go:271` all: mint a nonce,
then on verify just regex the nonce out of a **client-supplied** message and recover the address —
no exact-message / domain / expiry binding. A signature the user made on any other dapp (or a
phished sign) can be replayed to take over the grid account / chat session / gallery login.
→ Store nonce→exact-message server-side; require exact match + domain + expiry (EIP-4361). Fix all
three the same way. (Verified-good in all three: nonce single-use, EIP-191 recovery correct.)

### D. Worker-pay economics stated as "AIPG only" (~10 doc places) — it's USDC + AIPG
docs worker-llm/worker-media/run-a-node/tokenomics/index/grid-overview; website
Products/Services/about. → correct to "USDC + AIPG (~50/50 target)".

### E. Naming drift breaks the docs graph
`system-core`→`grid-core` (deployed-as name), `comfy-bridge`→`grid-media-worker`,
`aipg-sdk-*`→`grid-sdk-*`, phantom `aigarth-chatbot`. Root `AGENTS.md` (first file an agent reads)
has 5 dead links + 5 missing dirs. Hardcoded `/Users/j/fix-axios-vuln/...engineering-standards/`
absolute path in aipg-smart-contracts, grid-image-model-reference, validator-node.

## 4. P1 — will bite soon (by surface)

**grid-core:** unbounded Redis stream growth (jobs never XTRIM'd — memory leak carrying prompts);
empty-completion dead-letter silently drops job (client hangs); legacy-key path can register another
operator's worker name (affinity hijack + cooldown griefing); rate-limit keyed on unvalidated API key
(unthrottled 401 brute-force + DB-load DoS); STALE_JOB reclaim can re-dispatch a still-streaming job
(two workers interleave on one channel); media/raw error paths never `_record_strike` (dead-ComfyUI
worker never evicted); `den_earned`/`jobs_completed` inserted 0 and never incremented → **every
operator dashboard shows 0 earned forever** (demoralizes the workers you're recruiting); chat-routed
media skips the fast worker-availability check (300/600s hang instead of fast 503); stats/models/
progress routes have no rate limit at all (unauth DB-load DoS).

**text worker:** abandoned socket on some error paths keeps the grid thinking a dead worker is live;
120s httpx read timeout kills any >2min generation AND deregisters the worker; `WALLET_ADDRESS`
collected in wizard but **never transmitted** (operators think payouts are wired); post-`done` ack is
a blind recv that can swallow a pushed job frame.

**media worker:** SSRF in `download_image` (no scheme/host/size validation → LAN + 169.254.169.254 +
OOM/disk-fill); no cancel handling (cancelled 5-min video renders to completion); `allow_lora:true`
but no LoRA splicing exists → LoRA jobs silently return non-LoRA images and get paid; user-agent
spoofs "AI Horde Worker reGen" (misattributes fleet to db0's project — exactly what the acquisition
strategy avoids).

**aipg-chat:** anonymous `delete-all-chat-sessions` wipes ALL anonymous users' chats (shared anon
UUID); several mutating endpoints anon-writable (persona `upload_file` unauth write); image-gen edits
are **uncommitted working-tree only** (one `git checkout` from lost).

**gallery/console:** `POST /api/jobs` public + falls back to the house service key (anon drain of paid
account); gallery item ownership set from request body not JWT (spoofed attribution); console web3
sign-in `callbackUrl` unvalidated → open redirect; gallery Create page offers ONE image model + no
video (`styles.json`); JWT signature compared non-constant-time.

**website/docs:** `/api/v2` documented as supported (P0-adjacent); arc20→Token Factory missed on
website Timeline.js; "Verified on-chain" present-tense (validation not live); video listed "Q3
upcoming" though live; credits shown den-only with no USD/Base rails mentioned; dead FAQ.js with
horde/NFT-mint copy; a tracked `.page.js.swp` vim swap file.

## 5. Repo disposition

| Repo | Verdict |
|---|---|
| grid-core, grid-inference-worker, aipg-chat, aipg-art-gallery, grid-frontend(console), aipg-website, aipg-documentation, aipg-smart-contracts, validator-node, grid-sdk-js, grid-sdk-python, grid-media-worker, grid-discord-news-bot, aigarth-agent | **KEEP** (active) |
| grid-chat-new | **ARCHIVE** — superseded by aipg-chat; first confirm the SIWE web3-login work is captured in aipg-chat |
| grid-sdk | **ARCHIVE** — dead horde fork (already says so); flip GitHub archive bit; consider renaming so `grid-sdk` = the live Python SDK |
| grid-rewards-sentry | **ARCHIVE** — already deprecated, superseded |
| grid-discord-image-bot | **ARCHIVE** — scrapping (Justin) |
| grid-image-model-reference | **ARCHIVE or auto-generate** from `/v1/models` — 26 dead models, zero live |
| AIPG_Dev | **LEAVE to external owner** (mgillr, not the org) — mark not-prod |
| hyperdht, hyperswarm, py-libp2p, crdt-merge | **FREEZE** — p2p/CRDT research; premise review says cut the p2p program |

## 6. Premise review (zoom-out)

- **Coherence:** internal spine composes; public docs oversell. `autonomous-network.mdx` ("no central
  coordinator, no kill switch") describes a system that is one FastAPI box + an upgradeable Diamond
  with a single admin key. `pages/p2p/` documents a worker path that runs **unauthenticated,
  unmetered jobs from any gossip peer** — a hole wearing the boldest claim. TEE pages promise hardware
  you don't have.
- **Trust before money:** coordinator trusts worker self-report; no validator live. Not coherent for
  real money yet. Minimum before `GRID_CHARGING_ENABLED=1`: pre-flight balance gate, server-clamped
  den inputs + empty-output rejection, coordinator canary probes (PoQ-lite, no validators needed),
  refund path, real EIP-4361 SIWE.
- **Economic loop:** closed nowhere — charging dry-run, no funding rails, custodial payout CLI, reward
  contracts undeployed, treasury address owed. But the *metering* half (den, ledger, pricing) is real
  and tested.
- **Cut/freeze:** p2p program (+ hyperdht/hyperswarm/py-libp2p), AIPG_Dev, crdt-merge, both Discord
  bots (freeze), grid-chat-new (archive); finish killing the Flask horde (V2 step 6, 5/6 done).
- **Top SPOFs:** (1) key-person/custody — one founder holds SSH, DB, on-chain ADMIN_ROLE (can grant
  every role incl. itself), treasury, Vercel, CF; (2) single coordinator + single Postgres
  (create_all, not alembic) + Redis on hand-managed boxes; (3) content liability — CSAM gate is a
  placeholder VM while community workers render arbitrary prompts under the brand; (4) AIPG price on
  one thin Uniswap pool + contracts commingle bonds/rewards; (5) one R2 bucket/account.
- **What's genuinely good (protect):** ledger-first architecture (verifiability retrofittable without
  data-path redesign); validator V0 restraint (evidence has no teeth yet, in writing); "grid is the
  economic authority, front-ends are thin clients" (what makes 3 products sustainable for 1 person);
  the AGENTS.md DOX discipline (internal docs don't lie — is why this audit was possible).
- **Highest-leverage single move:** make **one real dollar** traverse the whole loop (Stripe → credit
  → pre-flight-gated metered job → ledger → Merkle epoch → USDC to one worker's wallet) before
  building anything else — fixing the billing gates as the price of admission — and cut the public
  story down to what's true (retire p2p/autonomous pages until they are).

## 7. Recommended execution order

1. **Rotate the gallery secrets** (§0) — today.
2. **grid-core P0s** (registry TTL refresh, token_stream dedup) — smallest fixes, biggest prod impact.
3. **`/v1` cutover of our own clients** — aigarth first (public README advertises broken features),
   then the two SDKs (`GridRaw`), then gallery checkout. Kill the /v2 doc sections in the same pass.
4. **Media-worker hardening** (auth the dashboard, default WS, wedge-fix, SSRF, cancel) before it
   touches a stranger's rig; ship the **text worker** to strangers now after the socket/ack fixes.
5. **SIWE binding** across the three auth surfaces (one fix pattern).
6. **Content sweep** — stale models + worker-pay-economics + arc20 + present-tense-decentralization
   across website & docs; then the AGENTS.md/naming alignment pass (root index first).
7. **Archive/label** the six repos; **freeze** the p2p program.
8. **Before charging on:** the billing-gate minimum bar (§6 trust).
