# SwarmFi · 2:45 demo video

## Pre-flight checklist (do BEFORE recording)

- [ ] Run `./start.sh --live` and let it complete one warm-up cycle so CoinGecko cache is populated → real prices show in the scan, not $0.00
- [ ] Confirm the dashboard's hero stats show a recent cycle with real risk score + real tx hash
- [ ] Open three browser tabs ahead of time:
  1. `http://127.0.0.1:8080` (the dashboard)
  2. `https://sepolia.etherscan.io/address/<KH_KEEPER_ADDRESS>` (proof of commitments)
  3. `https://chainscan-galileo.0g.ai/address/<ZG_WALLET>` (proof of snapshots)
- [ ] Record voiceover separately (phone is fine), mix in Audacity or DaVinci Resolve
- [ ] Resolution: 1920×1080 @ 30fps. Recorder: OBS Studio (free)

---

## Script — read at conversational pace, no rushing

### [0:00 – 0:15] Hook

> *"This is SwarmFi. Three autonomous AI agents trading DeFi together — without a central coordinator, with verifiable on-chain proof of every decision."*

**Visual:** dashboard loads, brand mark animates, hero shows the latest cycle stats with a green tx hash and 0G snapshot pill.

---

### [0:15 – 0:50] The scan

> *"Every cycle, the **researcher agent** scans live markets — four bluechip pairs on Base — and ranks them with a transparent composite edge profile. Momentum, bluechip preference, spread tightness, position size. Inspired by quant playbooks, auditable by anyone."*

**Visual:** scroll to **Live edge scan** table. Cursor hovers the coral ★ on the top row. The composite-edge bar fills.

> *"Top pick: ETH to USDC. 24-hour momentum 2.07%. Composite edge 0.87. Strong signal."*

---

### [0:50 – 1:30] Risk on 0G Compute

> *"That signal goes to the **risk agent**. It runs sealed AI inference on **0G Compute** — every signal scored zero to ten. Above the threshold, the swarm holds."*

**Visual:** click **▶ Run a cycle**. Narrator pill flips through:

- *"Scanning markets — ETH → USDC…"*
- *"Risk agent scoring on 0G Compute… Got 2.0/10."*

Risk dial smoothly tweens to 2.0.

> *"Two out of ten. Bluechip pair, healthy market — risk approves a buy with 100% confidence."*

---

### [1:30 – 2:00] Execution: Uniswap + KeeperHub

> *"The **executor** consults the **Uniswap Trading API** as a live price oracle — exact rate, gas estimate, best route. Then commits the swarm's decision on-chain through **KeeperHub** for guaranteed delivery."*

**Visual:** narrator pill: *"Executing — Uniswap quote → KeeperHub broadcast…"* Then settles to the BUY one-liner with the tx hash.

> *"Real on-chain transaction on Sepolia."*

**Visual:** click the tx hash in the hero. **Sepolia Etherscan** opens — the confirmed transaction with the keeper wallet's signature.

---

### [2:00 – 2:30] 0G Storage proof + ENS identity

> *"Every cycle — every signal, every decision, every tx — is committed to **0G Storage** as one verifiable snapshot."*

**Visual:** click the **0G snapshot** pill in the header. Chainscan Galileo loads — the registered flow tx.

> *"Each agent has its own **ENS identity**. researcher.swarmfi.eth. risk.swarmfi.eth. executor.swarmfi.eth. The dashboard never hardcodes addresses — every agent's metadata is resolved through ENS at runtime, with text records that update every cycle."*

**Visual:** scroll to **Agent identities · ENS profiles** panel. Show the resolved addresses + the live `swarmfi.last`, `swarmfi.tx`, `swarmfi.snapshot` records.

---

### [2:30 – 2:45] Close

> *"Five sponsor primitives, one autonomous swarm. **0G Storage and Compute. Uniswap. KeeperHub. Gensyn AXL. ENS.** Each one doing real work. Every cycle on-chain. SwarmFi."*

**Visual:** zoom out to a hero shot of the dashboard with all panels populated. Logo. End.

---

## Filming tips

- **Talk slower than feels natural.** First-time viewers need to absorb both audio and visuals.
- **Highlight clicks** with a click-indicator overlay (OBS plugin or native macOS `Mouse Cursor Highlighter`).
- **Cut, don't stitch.** Re-record full takes rather than splice — pacing flows better.
- **Captions matter.** Auto-caption with YouTube or [Captions.app](https://captions.app); judges might watch muted.
- **End frame should be the dashboard with the snapshot pill visible** — it's your strongest single visual.
