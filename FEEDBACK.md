# Uniswap API Feedback — SwarmFi (ETHGlobal OpenAgents 2026)

> This file is required for Uniswap Foundation prize eligibility.
> It documents our honest experience integrating the Uniswap Trading API
> into an autonomous multi-agent DeFi swarm during the hackathon.

---

## What We Built

SwarmFi is a three-agent autonomous swarm where a researcher agent detects
market signals, a risk agent scores them via 0G Compute, and an executor
agent executes the swap through the Uniswap Trading API + KeeperHub.
The Uniswap API is the execution layer for all swap activity.

---

## What Worked Well

**The `/quote` + `/swap` flow is clean and well-designed.**
The separation between quoting and building the transaction is exactly
what an agent needs — quote when the signal arrives, build tx when
risk approval comes back. The `routing` field in the quote response
cleanly determines whether to use `/swap` or `/order`, removing any
guesswork about which endpoint to call.

**Permit2 flow is documented clearly.**
The explicit note that both `signature` AND `permitData` must be
omitted together (not just one) saved time. This is the kind of
footgun that trips up integrators silently.

**The `data` field validation guidance is excellent.**
The explicit warning to never modify or allow an empty `data` field
is the most important thing in the docs and it's prominently placed.
We added a validator in our Pydantic model to enforce this at the
Python layer.

**Mock-ability.**
The API is pure REST with no SDK required, which made it trivial to
build a mock backend for our unit tests. Our 71 passing unit tests
run without any API key.

---

## Pain Points & Bugs

### 1. No official Python SDK
**Impact: High.** Every other integration in our stack (0G Storage,
AXL, web3) has a Python client. We had to build our own Pydantic
wrapper from scratch using the OpenAPI spec. For hackathons specifically,
a Python SDK would dramatically reduce integration time.

**Request:** Publish `uniswap-trading-py` to PyPI, even if it's
a thin auto-generated wrapper from the OAS.

---

### 2. Quote response schema is inconsistent between routing types
**Impact: Medium.** For CLASSIC routing, `outputAmount` is nested inside
`quote.outputAmount.amount`. For DUTCH_V2, the structure differs.
We had to handle both cases in our response parser.

**Specific issue:** The OAS at `https://trade-api.gateway.uniswap.org/v1/api.json`
shows a generic `object` type for `quote` rather than discriminated union
types per routing. This makes it hard to write type-safe code.

**Request:** Add discriminated union types to the OAS keyed on `routing`.

---

### 3. `/check_approval` response field name unclear
**Impact: Low.** The docs show `needsApproval` as the field, but we had
to verify this in practice because the OAS uses `approval` inconsistently
in some examples. A minimal working example in the docs would help.

---

### 4. No testnet / sandbox environment
**Impact: High for hackathons.** We spent time on Base mainnet with real
funds during development because there is no sandbox. A Sepolia or Base
Sepolia supported endpoint (even with rate limiting) would make hackathon
development much safer and faster.

**Request:** Add `https://trade-api-testnet.gateway.uniswap.org/v1/`
pointing to Sepolia/Base Sepolia for development use.

---

### 5. Quote expiry is not returned explicitly
**Impact: Medium.** The docs recommend refreshing quotes older than 30s,
but the quote response doesn't include an explicit `expiresAt` timestamp.
Agents have to track quote creation time themselves.

**Request:** Add `expiresAt: number` (unix ms) to the quote response.

---

### 6. UniswapX order status polling not straightforward
**Impact: Medium.** For DUTCH_V2/V3/PRIORITY routing, the swap is gasless
but we need to poll `GET /orders?orderId=...` to confirm fill. The polling
interval and timeout guidance in the docs is vague.

**Request:** Add recommended polling interval and timeout guidance to the
`/orders` docs section.

---

## Missing Features We Wanted

1. **Webhook / push notification for order fills** — polling `/orders` from
   an agent loop is inefficient. A callback URL on `POST /order` would let
   executor agents react immediately on fill.

2. **Batch quotes** — our researcher agent wants to evaluate multiple
   opportunities simultaneously. A `POST /quotes` (plural) endpoint that
   accepts an array would reduce API calls significantly.

3. **Historical price for a pair** — useful for the risk agent to assess
   whether the current price is an outlier. Even a simple `GET /price`
   endpoint returning TWAP would be valuable.

---

## Documentation Gaps

- The `swapping-code-examples` page has JS examples only. Python examples
  (even pseudocode) would help the significant portion of agent builders
  using Python.

- The `building-prerequisites` page mentions "Web3 Library" but doesn't
  give a Python-specific recommendation. `web3.py` works fine but isn't
  mentioned anywhere.

- Error response body format is documented but the actual `error` string
  values aren't enumerated. We had to trial-and-error common errors.

---

## Overall Assessment

The Uniswap Trading API is the best way to get production-grade swap
execution into an agent. The REST-first design with no required SDK is
the right call for multi-language agent ecosystems. The main gap for
hackathon developers is the lack of a testnet environment and Python SDK.

**Rating: 8/10** — excellent API, Python ecosystem support needed.

---

*Team: SwarmFi | ETHGlobal OpenAgents 2026*
*Contact: [Telegram] [X]*