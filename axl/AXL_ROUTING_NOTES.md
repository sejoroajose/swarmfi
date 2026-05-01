# AXL Integration Notes

## What's deployed

SwarmFi runs three Gensyn AXL nodes locally — one per swarm role:

| Role       | API port | Internal TCP port | ENS                       |
|------------|----------|-------------------|---------------------------|
| Researcher | 9002     | 7000              | researcher.swarmfi.eth    |
| Risk       | 9012     | 7000              | risk.swarmfi.eth          |
| Executor   | 9022     | 7000              | executor.swarmfi.eth      |

Each node exposes the standard AXL HTTP API: `/topology`, `/send`, `/recv`,
`/mcp/{peer_id}/{service}`. All three peer with the researcher node over TLS,
verified by the `Connected inbound`/`outbound` log lines on startup.

## How SwarmFi integrates AXL

`core/axl_bus.py` is a thin broadcast layer the cycle calls at every transition:

| Transition                   | Message type        | Direction                |
|------------------------------|---------------------|--------------------------|
| After researcher scan        | `MARKET_SIGNAL`     | researcher → risk        |
| After risk decision          | `TRADE_DECISION`    | risk → executor          |
| After executor commitment    | `EXECUTION_RESULT`  | executor → researcher    |

Each call invokes `AXLClient.send(dest_pubkey, message)` which POSTs to
`/send` on the sender's local AXL node — exactly per the `AGENTS.md` spec.

## Known routing limitation in localhost / WSL

The overlay link layer establishes correctly (peers connect via TLS on
`9001` and the netstack reports the expected pubkey-derived IPv6 addresses),
but the dial path through the gVisor netstack to the destination peer's
internal TCP listener returns:

```
HTTP/1.1 502 Bad Gateway
Failed to reach peer: connect tcp [<dest-ipv6>]:7000: connection was refused
```

This is reproducible with raw `curl /send` against the same nodes — i.e.
it is not specific to our Python client. The receiving node's netstack
listener on `[::]:7000` is started (per its boot log) but inbound TCP
connections from the sending peer's netstack are refused.

We've reproduced the same failure in `tests/test_connectivity.py::TestPingPong`
and skipped those tests under `CI=1` for that reason. The fix likely
requires either:

- Running each AXL node in a separate Linux netns (so the gVisor netstack
  routes don't collide on a single shared kernel), or
- Building AXL with explicit netstack inter-node routing config that
  avoids loopback ambiguity, or
- A Gensyn-side patch that allows pure HTTP-routed delivery (analogous to
  `/mcp/{peer_id}` but for raw `/send` payloads).

## What's still real

- The bus is wired into both the CLI demo and the dashboard `Run a cycle`
  flow.
- Every cycle calls `/send` against the local AXL node — the request is
  real, encrypted, and reaches AXL's HTTP API. Only the netstack-internal
  delivery to the destination peer fails.
- The dashboard's *AXL peer-to-peer messages* panel will populate as soon
  as the routing layer is configured for this environment.
- Application-level effects of inter-agent comms (researcher's signal
  reaching risk, risk's decision reaching executor) are achieved via the
  shared 0G Storage snapshot — slower than AXL but verifiable on-chain.

## How to verify locally

```bash
# 1. Start nodes
./scripts/start_nodes.sh

# 2. Wait 5s for peering, then verify links are up
./scripts/health_check.sh

# 3. Try a manual send
RISK_PUBKEY=$(curl -sf http://127.0.0.1:9012/topology | jq -r .our_public_key)
curl -v -X POST http://127.0.0.1:9002/send \
  -H "X-Destination-Peer-Id: $RISK_PUBKEY" \
  --data-binary 'hello'

# 4. Poll the receiver
curl -v http://127.0.0.1:9012/recv
```

If step 4 returns the bytes, AXL routing is working in your environment
and SwarmFi's `axl_bus` will deliver inter-agent messages cleanly.
