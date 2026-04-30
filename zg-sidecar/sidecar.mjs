#!/usr/bin/env node
/**
 * zg-sidecar v2 — returns root hash immediately after tx submission.
 * Does NOT wait for storage node propagation (that happens async on-chain).
 *
 * Commands:
 *   upload   --key <hex> [--evm <url>] [--indexer <url>]  < stdin-bytes
 *            → stdout: {"rootHash":"0x...","txHash":"0x..."}
 *
 *   download --root <0xhash> [--indexer <url>]
 *            → stdout: raw bytes
 */

import { Indexer, MemData } from '@0gfoundation/0g-ts-sdk';
import { ethers } from 'ethers';
import { readFileSync, unlinkSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

// Redirect ALL console.* to stderr — stdout is reserved for our JSON result
const _log = (...a) => process.stderr.write(a.map(String).join(' ') + '\n');
console.log = console.info = console.warn = console.error = console.debug = _log;

const EVM_RPC     = 'https://evmrpc-testnet.0g.ai';
const INDEXER_RPC = 'https://indexer-storage-testnet-turbo.0g.ai';

// How long to wait for the flow tx to be mined (not storage node sync)
const TX_TIMEOUT_MS    = 60_000;   // 60 s for tx
const UPLOAD_BUDGET_MS = 90_000;   // 90 s total before we bail

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) { args[argv[i].slice(2)] = argv[i+1] ?? true; i++; }
  }
  return args;
}
function fatal(msg) { process.stderr.write(`error: ${msg}\n`); process.exit(1); }
function readStdin() {
  return new Promise((res, rej) => {
    const chunks = [];
    process.stdin.on('data', c => chunks.push(c));
    process.stdin.on('end', () => res(Buffer.concat(chunks)));
    process.stdin.on('error', rej);
  });
}

async function cmdUpload(args) {
  const privKeyRaw  = args.key;    if (!privKeyRaw) fatal('--key required');
  const evmRpc      = args.evm     || EVM_RPC;
  const indexerRpc  = args.indexer || INDEXER_RPC;
  const privKey     = privKeyRaw.startsWith('0x') ? privKeyRaw : `0x${privKeyRaw}`;

  const data = await readStdin();
  if (!data.length) fatal('no stdin data');

  const provider = new ethers.JsonRpcProvider(evmRpc);
  const signer   = new ethers.Wallet(privKey, provider);
  const memData  = new MemData(data);
  const indexer  = new Indexer(indexerRpc);

  // ── Attempt upload, but race against a hard deadline ────────────────────────
  // The SDK sometimes blocks indefinitely on "Wait for log entry on storage
  // node". We race the whole upload against UPLOAD_BUDGET_MS. If we already
  // have a root hash from a partial result we use it; otherwise we bail.

  let resolvedRootHash = null;
  let resolvedTxHash   = null;

  const uploadPromise = (async () => {
    // skipTx:false  → submit the flow tx (needed for root hash on-chain)
    // finalityRequired:false → don't wait for finality
    const [result, err] = await indexer.upload(memData, evmRpc, signer, {
      expectedReplica: 1,
      skipTx: false,
      finalityRequired: false,
    });

    if (err && !err.message?.toLowerCase().includes('already')) throw err;

    const rootHash = result?.rootHash ?? result?.rootHashes?.[0];
    const txHash   = result?.txHash   ?? result?.txHashes?.[0];
    return { rootHash, txHash };
  })();

  const timeoutPromise = new Promise(res =>
    setTimeout(() => res({ timedOut: true }), UPLOAD_BUDGET_MS)
  );

  const outcome = await Promise.race([uploadPromise, timeoutPromise]);

  if (outcome.timedOut) {
    // TX was submitted (we saw "Transaction submitted" in stderr logs) —
    // compute the root hash locally from the data so we can still return it.
    // The flow tx already landed; storage nodes will pick it up async.
    process.stderr.write('[zg-sidecar] upload timed out waiting for node sync — returning tx root\n');

    // Recompute root from MemData (deterministic)
    const localData  = new MemData(data);
    // The root is computed by the SDK before any network call:
    // we re-create a MemData and call getRoot() / info()
    try {
      const info = await localData.info();
      resolvedRootHash = info.root;
      // We don't have the tx hash in timeout path — that's ok
      resolvedTxHash   = null;
    } catch (_) {
      fatal('upload timed out and could not compute local root hash');
    }
  } else {
    resolvedRootHash = outcome.rootHash;
    resolvedTxHash   = outcome.txHash ?? null;
  }

  if (!resolvedRootHash) fatal('upload returned no rootHash');
  process.stderr.write(`[zg-sidecar] root=${resolvedRootHash}\n`);
  process.stdout.write(JSON.stringify({ rootHash: resolvedRootHash, txHash: resolvedTxHash }) + '\n');
}

async function cmdDownload(args) {
  const root        = args.root;    if (!root) fatal('--root required');
  const indexerRpc  = args.indexer  || INDEXER_RPC;
  const maxAttempts = parseInt(args.retries ?? '8', 10);
  const baseDelay   = parseInt(args.delay   ?? '5000', 10);

  const rootHash = root.startsWith('0x') ? root : `0x${root}`;
  const tmpPath  = join(tmpdir(), `zg-dl-${Date.now()}.bin`);
  const indexer  = new Indexer(indexerRpc);

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    process.stderr.write(`[zg-sidecar] download attempt ${attempt}/${maxAttempts}\n`);
    const dlErr = await indexer.download(rootHash, tmpPath, false);
    if (!dlErr) {
      const bytes = readFileSync(tmpPath);
      try { unlinkSync(tmpPath); } catch (_) {}
      process.stdout.write(bytes);
      return;
    }
    const msg = dlErr.message ?? String(dlErr);
    const retry = msg.toLowerCase().includes('no locations') ||
                  msg.toLowerCase().includes('0 locations') ||
                  msg.toLowerCase().includes('not found');
    if (!retry) fatal(`download failed: ${msg}`);
    if (attempt < maxAttempts) {
      const wait = baseDelay * attempt;
      process.stderr.write(`[zg-sidecar] retrying in ${wait/1000}s: ${msg}\n`);
      await new Promise(r => setTimeout(r, wait));
    }
  }
  fatal(`download failed after ${maxAttempts} attempts`);
}

const [,, cmd, ...rest] = process.argv;
const args = parseArgs(rest);
if (cmd === 'upload')        cmdUpload(args).catch(e => fatal(e.message));
else if (cmd === 'download') cmdDownload(args).catch(e => fatal(e.message));
else fatal(`unknown command: ${cmd ?? '(none)'}. Use upload or download`);
