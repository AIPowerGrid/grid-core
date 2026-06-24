# Grid Economics — Demand Side, Developer Incentives & Funding

> The last revolution was metered in kilowatt-hours. This one is metered in tokens.

This is the blueprint for the money layer of the AI Power Grid. The governing
principle, and the thing that makes everything else fall into place:

> **The grid is the economic authority. Front-ends are thin clients.**

Pricing, metering, credits, limits, funding, payouts, and revenue-share all live
in the grid (`grid_api`). The chat (aipg.chat), the gallery (aipg.art), the
developer console (console.aipowergrid.io), third-party apps, and autonomous
agents are *clients* — they authenticate a user to a grid account, make calls,
and render balance + upsell UI. **No billing logic ships in a front-end.** Build
it once; every surface inherits it.

This doc covers the **demand side** (who pays, how) and the **developer layer**
(who builds and profits). The **supply side** — workers earning for compute —
is in the settlement docs; this doc references where the two planes meet.

---

## 0. Thesis & positioning (read this first)

We stress-tested this against the mission and cut the part that didn't survive.

**What we are NOT building:** a cheaper ChatGPT. Competing on "half the price"
in the consumer chat market is a no-moat race to the bottom against subsidized
incumbents. A standalone $10/mo chat subscription has no story.

**What we ARE building:** a permissionless, agent-native compute network whose
moat is **decentralized supply** (independent GPU workers earning) and whose
demand is **machine-native** (agents paying per call, no signup). One pricing +
metering engine in the grid; everything else is a door onto it.

Two doors, one credits core:

- **The Pass** — humans buy one ecosystem pass (Stripe or crypto) → credits
  spendable across *every* AIPG front-end (chat + gallery + API). Positioned as
  *"everything AIPG, one pass, funding a decentralized network — not a megacorp,"*
  **never** as "cheap AI." It's an ecosystem bundle and a token sink, and it
  closes the loop by funding worker payouts.
- **x402** — agents pay per call in USDC on Base, account-less. The
  differentiated, on-brand wedge and the headline of the *story*.

Discipline this implies:
- The pricing/metering engine is the only thing that must be excellent. The Pass
  is just `Stripe → credit grant` on top of it (cheap). x402 is the same meter
  with a different settlement.
- Keep the consumer **funnel lean** — free with a sane limit → "get the Pass /
  connect a wallet." Do not build a conversion-optimization machine into a chat
  wrapper.
- **Lead the messaging with the network + agent story**, not the Pass. Chat and
  gallery are the shop window, not the cash register.
- The one piece required either way, and worth doing well: the **identity
  keystone** (every front-end user resolves to one grid account).

---

## 1. Actors and value flows

```
   CONSUMERS ──pay──▶ ┌─────────────────────────────┐
   (chat, gallery,    │           THE GRID          │
    apps, agents)     │  accounts · credits · meter │
                      │  pricing · limits · payouts │
   DEVELOPERS ──build─▶│                             │──earn──▶ DEVELOPERS
   (apps, agents,     │  one USD balance per account│         (rev-share / margin)
    styles, recipes)  │                             │
                      │                             │──earn──▶ WORKERS
   WORKERS ──compute──▶└─────────────────────────────┘         (USDC + AIPG, supply side)
                                    │
                                 protocol fee
                                    │
                               AIPG buyback / treasury
```

Four ways to make money *with* the grid, one way the grid makes money:

- **Consumers** pay (fiat or crypto) to use chat / gallery / the API.
- **Developers** build apps and agents on the grid and **keep a margin** on the
  usage they drive.
- **Creators** publish styles / recipes / models and earn a royalty per use.
- **Workers** provide GPU compute and earn (USDC + AIPG) — supply side.
- **The protocol** takes a fee on throughput; a slice funds AIPG buybacks
  (see the treasury & buyback policy, §7).

Everyone settles against **one ledger, one balance per account**.

---

## 2. Identity — the keystone

Every user, on every surface, resolves to **one grid account**. This is the
single hardest and most important piece: without it, each front-end meters in
its own silo and nothing unifies.

| Surface | Identity today | Bridge needed |
| --- | --- | --- |
| Console | OAuth/SIWE → grid account ✅ | done |
| Gallery | grid `/v1` | confirm per-user account, not shared key |
| Chat (Onyx) | own users, **one shared grid key** | provision a grid account per chat user; chat authenticates with a **scoped bridge key** + a **short-lived signed user assertion** (not a raw header, not the user's raw key threaded through LiteLLM) — see DEMAND_SIDE_AUDIT_BRIEF §2 / B2+B3 |
| Third-party apps | their API key = a grid account | already native |
| Agents | API key / x402 wallet | native via x402 |

The chat bridge mirrors what the console already does: on sign-in, find-or-create
a grid account (internal-token session). Inference is then submitted by a scoped
bridge key carrying a signed assertion for that user (B2+B3), not the user's raw
key — equivalent attribution, safer trust model. (Earlier drafts said "send with
their own key"; superseded by the audit.) Route that user's inference with
their own key. The moment a user is a grid account, **metering, credits, limits,
and revenue-share all work for them automatically, everywhere.**

---

## 3. Credits — the unit of account

- One **USD balance** per account, integer **micro-USD** (`grid_credits` /
  `grid_credit_ledger`). Append-only ledger is truth; balance is a cache.
  Built — see `services/credits.py`.
- Why USD, not AIPG: USDC is what people and agents hold on Base and what x402
  settles in. AIPG is the worker-stake / reward asset, not the customer unit.
- Spent across **chat, gallery, and the API** — one wallet, every surface.

---

## 4. Metering, tiers & enforcement (all grid-side)

The grid prices every request (`services/pricing.py`, "half the cheapest
competitor") and enforces in the request path (chat metering already wired,
dry-run). Tiers are **entitlements on the account**, not front-end logic:

| Tier | Limit | Mechanism |
| --- | --- | --- |
| **Anonymous** | ~5 msgs / session | per-session + per-IP counter (Redis); also the abuse guardrail |
| **Free (registered)** | ~25 msgs / day | per-account daily counter (free-tier quota exists, #35) |
| **Plus ($10/mo)** | monthly credit pool across chat **+** gallery + API | Stripe sub → monthly credit grant |
| **Pay-as-you-go** | spend your balance | top up any rail below |

At a limit, the grid returns a structured "needs upgrade / out of credit"
response; front-ends render the upsell (create account → more free → go Plus).
The numbers are config knobs (`services/economics.py`), tunable without a deploy.

---

## 5. Funding rails — many doors, one balance

Every rail does the same thing: **land value → credit the account's USD
balance.** They're independent adapters; adding one never touches the others.

- **Stripe (fiat)** — the $10/mo subscription and one-time top-ups. Webhook on
  `invoice.paid` / `checkout.session.completed` → `credits.credit(account, micro,
  ref=event.id)` (idempotent). Easiest recurring path; lowest friction for
  "card → credits."
- **USDC on Base** — deposit to treasury → watcher reads `Transfer` → credit 1:1.
- **ETH / cbBTC on Base** — same watcher; swap to USDC at the door, credit the
  proceeds. (cbBTC = "BTC on Base".)
- **AIPG on Base** — accepted at peg, optionally with a bonus to drive token
  demand; **never swapped to USDC** (no self-inflicted sell pressure). See §7
  for the buyback flywheel.
- **x402** — agent pay-per-call (USDC on Base, EIP-3009). The per-request meter
  *is* the x402 price; no prepaid balance needed. The agent-native flagship.
- **AP2** (Agent Payments Protocol) — mandate-based agent payments; pairs with
  x402 for delegated, auditable agent spend. Future, grid-native.

All AIPG/ETH/BTC pricing reads the AIPG/USDC Uniswap v4 pool on Base as a TWAP
(the same venue any buyback executes against).

---

## 6. Developer layer — build and profit on the grid

This is what turns the grid from a cheap API into a **platform developers have a
reason to build on**. A developer building an app or agent on the grid can make
money four ways, all settled by the grid:

### 6a. Reseller margin (the headline)
A developer builds an app, sets their **own retail price**, and the grid bills
their end-users the retail price, pays the developer the **spread**, and keeps
the wholesale cost. Because wholesale is already "half the cheapest competitor,"
the developer has real room to price and still undercut the market.

- Grid charges end-user `retail`; grid keeps `wholesale + protocol_fee`; developer
  earns `retail − wholesale − protocol_fee`.
- The developer never touches infrastructure, billing, or payments — they ship a
  product and collect a margin.

### 6b. Revenue share on referred usage
For developers who don't want to run their own billing: tag traffic with an
**app/partner id**, and the developer earns a configurable **% of the spend they
drive** — affiliate-style, paid into their balance.

### 6c. Creator royalties (marketplace)
Publish a **style, recipe, model, or tool/agent**; earn a per-use royalty when
anyone on the grid uses it. This is the StyleVault / recipe-vault path
(`services/styles.py`, `services/recipes.py`) extended to a paid marketplace.

### 6d. Agent services (x402 / AP2)
A developer's **agent or tool can charge for its own service** on top of grid
compute — the agent exposes an x402-priced endpoint, the grid handles settlement,
the developer keeps the service fee. The grid becomes the payment rail for an
agent economy, not just the compute.

### Attribution & payouts (what makes 6a–6d work)
- **Attribution**: every request can carry an `app_id` / partner key →
  the grid attributes usage and revenue to the developer's account. Same ledger,
  new dimension.
- **Earnings** accrue to the developer's balance as credits, **withdrawable in
  USDC** (same payout path as workers — `payout_wallet`, settlement).
- **Why a developer builds here**: half-price wholesale to build on, keep your
  own margin, get paid in stable USDC, and agent-native rails (x402/AP2) out of
  the box. Plus: the cheapest world-class inference to build against in the first
  place.

---

## 7. The protocol cut & the token

- Throughput carries a **protocol fee** (the demand-side analog of the supply
  split). Developer margin/rev-share comes out of the headroom between wholesale
  and retail, not the worker's cut.
- A slice of protocol revenue funds **on-market AIPG buybacks** (never
  founder-OTC), tying network usage to token demand (see the treasury & buyback policy, §7).
- **AIPG** = worker stake (bonding/slashing, #59) + governance + value capture.
  It is deliberately **not** the customer unit of account — customers and
  developers think in USD; the token captures the network's success underneath.

---

## 8. Built vs. to-build

**Built**
- Accounts + identity (console path), API keys, payout_wallet
- Credits (USD balance + idempotent ledger) — dry-run
- Per-model pricing (`pricing.py`); economics knobs (`economics.py`)
- Chat completion metering in the request path — dry-run
- Free-tier daily quota (#35); per-key rate limiting (#36)
- Supply-side settlement (workers): ledger, Merkle, on-chain reward facets

**To build**
1. **Identity bridge** — chat (Onyx) + gallery users → grid accounts (§2).
2. **Tiered entitlements** — anon / free / Plus / PAYG as grid-side rules (§4).
3. **Funding rails** — Stripe → credit; Base deposit watcher (USDC/ETH/AIPG);
   x402 endpoint; AP2 later (§5).
4. **Developer layer** — attribution → reseller-margin / rev-share → dev payouts;
   then marketplace royalties (§6).
5. **Thin front-end UX** — balance widget + upsell CTAs in chat/gallery (no logic).

---

## 9. Build sequence

The keystone first, because everything downstream needs it:

1. **Identity bridge + tiered enforcement (grid).** Once chat/gallery users are
   grid accounts and the grid meters them per-user, limits enforce themselves and
   every funding rail just credits one balance.
2. **Stripe → credits** + the $10/mo Plus plan (fastest revenue; fiat is easiest
   recurring).
3. **Base deposit watcher** (USDC → ETH/AIPG) and **x402** endpoint (parallel,
   independent adapters).
4. **Developer layer** — attribution + reseller margin / rev-share + dev payouts.
5. **Marketplace royalties** (styles/recipes/models/agents).

Flip from dry-run to live (`GRID_CHARGING_ENABLED=1`) only after a rail can fund
balances and the limits/observability are proven against real traffic.

---

## 10. Open decisions

- **Developer model**: reseller-margin, revenue-share, or both? (Recommend both —
  margin for product builders, rev-share for referrers.)
- **Exact numbers**: anon/free limits; what $10/mo grants (credit pool size);
  protocol fee %; default dev rev-share %.
- **Marketplace scope/timing**: which assets first (styles? agents?).
- **AP2 adoption**: build now or after x402 has traction.
- **Unify-now vs chat-first**: ship the chat funnel against the grid keystone, or
  wait for all rails — recommend keystone + Stripe first, rails in parallel.

---

*Related (code + docs): `services/credits.py`, `services/pricing.py`,
`services/economics.py` (demand-side primitives); ECONOMICS.md (supply-side
split); `services/styles.py` / `services/recipes.py` (marketplace primitives);
the settlement docs (worker payout).*
