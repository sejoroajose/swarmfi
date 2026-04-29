# KeeperHub Builder Feedback — SwarmFi (ETHGlobal OpenAgents 2026)

> Submitted for the KeeperHub Builder Feedback Bounty ($250).
> Honest, actionable feedback from integrating KeeperHub as the
> guaranteed execution layer for an autonomous multi-agent DeFi swarm.

---

## What We Built

SwarmFi routes all onchain execution through KeeperHub. The executor
agent receives trade decisions from the risk agent, builds the swap
transaction via the Uniswap Trading API, then hands the raw calldata
to KeeperHub's `execute_contract_call` endpoint. KeeperHub handles
gas estimation, nonce management, retry logic, and the audit trail.

**Integration depth:** `execute_contract_call` for Uniswap Universal
Router calls, `execute_transfer` for agent-to-agent value transfers,
`create_workflow` for persistent automation, and `get_execution_status`
for polling confirmation.

---

## What Worked Well

**The direct execution API is clean.**
`execute_contract_call` with `calldata` + `value` is exactly the right
abstraction for passing raw Uniswap transaction data through. We don't
need to re-parse the ABI — we just forward the bytes. This is the
correct design for an agent execution layer.

**The MCP server is a genuinely good idea.**
The fact that an agent can call KeeperHub tools via MCP without any
custom SDK is a real differentiator. For our AXL-based swarm it was
slightly harder to use (we call the REST API directly from Python),
but for any Claude Code or LangChain-based agent this would be the
obvious integration path.

**Gas optimization is meaningful.**
The ~30% gas savings claim is credible. In a production swarm running
many swaps, this compounds significantly. Good selling point for the
DeFi use case.

**Audit trail is what agents need most.**
Every execution having `execution_id`, `tx_hash`, `gas_used`,
`block_number`, and `explorer_url` in a single response is exactly
what a swarm needs to log to 0G Storage. No post-processing required.

---

## Pain Points & Bugs

### 1. No Python SDK — highest friction point
**Impact: High.**
Every other integration in our stack has a Python client. We had to
write our own Pydantic wrapper and REST client from scratch. For a
hackathon this cost approximately 3 hours.

**Specific issue:** The API docs at `docs.keeperhub.com` returned 403
when fetched programmatically (curl works, but our documentation
scraper didn't). We had to use the GitHub MCP README as the primary
reference.

**Request:** Publish `keeperhub-py` to PyPI. Even a thin auto-generated
wrapper from the OpenAPI spec would eliminate this friction entirely.

---

### 2. `execute_contract_call` with raw calldata is underdocumented
**Impact: High.**
The docs show `functionName` + `functionArgs` as the primary interface.
The `calldata` field (for passing raw hex from other APIs like Uniswap)
is mentioned but not shown with a working example. We had to infer the
correct field name from context.

**Reproducible steps:**
1. Get a raw transaction from the Uniswap Trading API (`POST /swap`)
2. Try to pass `tx.data` to KeeperHub
3. Unclear whether to use `calldata`, `data`, `functionArgs`, or ABI parsing

**Request:** Add a dedicated example in the docs for "passing raw
calldata from another protocol" — this is a very common agent pattern.

---

### 3. Network name strings are not documented exhaustively
**Impact: Medium.**
The docs show `"ethereum"`, `"polygon"`, `"base"` as examples but
don't list all valid network strings. We didn't know if `"base-sepolia"`
or `"baseSepolia"` or `"base_sepolia"` was correct until we tried.

**Request:** Add a reference table of all supported network name strings
with their chain IDs.

---

### 4. No webhook / callback for execution completion
**Impact: Medium.**
Agents have to poll `GET /executions/{id}` to know when a transaction
confirms. In a swarm with multiple concurrent executions this creates
polling overhead. A `callbackUrl` parameter on direct execution calls
would let the executor agent react immediately.

**Request:** Add optional `callbackUrl` to direct execution endpoints.
The callback body should include the full execution status object.

---

### 5. `docs.keeperhub.com` returns 403 for programmatic access
**Impact: Low but notable.**
We couldn't fetch the docs from our CI environment or via curl in some
configurations. Docs should be publicly accessible without CORS or bot
restrictions.

**Reproducible:**
```bash
curl https://docs.keeperhub.com/api
# 403 Forbidden
```

---

### 6. No way to test without a funded wallet
**Impact: Medium for hackathons.**
There's no sandbox mode that returns mock tx hashes. Getting a Sepolia
wallet funded and configured in KeeperHub before you can test anything
adds setup friction. A `dry_run: true` flag on execution endpoints that
returns a mock tx hash would be valuable for CI and development.

**Request:** Add `"dryRun": true` to all execution endpoint bodies.

---

## Feature Requests

1. **Batch execution** — submit multiple `execute_contract_call` items
   in one request. For a swarm executing several small trades, batching
   would reduce API calls and total latency.

2. **Execution webhooks** — covered above, would be the single highest
   impact addition for agent use cases.

3. **Python SDK** — covered above.

4. **Execution cost estimate** — a `POST /estimate` endpoint that returns
   expected gas cost and USD value before submitting. Useful for the
   risk agent to gate trades based on gas economics.

5. **Retry configuration per request** — ability to specify max retries
   and backoff strategy per execution, not just globally. Some agent
   actions are time-sensitive; others can retry indefinitely.

---

## Overall Assessment

KeeperHub is filling a real gap. The core value proposition — "agents
handle strategy, we guarantee the transaction lands" — is exactly right.
The REST API is well-structured and the concepts are sound. The main
gaps for Python-based agent builders are the missing SDK and the
calldata documentation.

**Rating: 7.5/10** — excellent product concept, Python ecosystem
support and calldata docs are the two things that would make this
genuinely frictionless for hackathon builders.

---

*Team: SwarmFi | ETHGlobal OpenAgents 2026*
*Contact: [Telegram] [X]*