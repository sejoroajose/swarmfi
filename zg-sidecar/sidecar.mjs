#!/usr/bin/env node
/**
 * zg-sidecar: Node.js CLI that wraps @0gfoundation/0g-ts-sdk.
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

// ── Redirect ALL console output to stderr ─────────────────────────────────────
// The 0G SDK calls console.log extensively. stdout is reserved exclusively for
// our binary/JSON result — any console output there corrupts it.
const _log = (...a) => process.stderr.write(a.map(String).join(' ') + '\n');
console.log   = _log;
console.info  = _log;
console.warn  = _log;
console.error = _log;
console.debug = _log;
// ─────────────────────────────────────────────────────────────────────────────

const EVM_RPC     = 'https://evmrpc-testnet.0g.ai';
const INDEXER_RPC = 'https://indexer-storage-testnet-turbo.0g.ai';

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      args[argv[i].slice(2)] = argv[i + 1] ?? true;
      i++;
    }
  }
  return args;
}

function fatal(msg) {
  process.stderr.write(`error: ${msg}\n`);
  process.exit(1);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on('data', c => chunks.push(c));
    process.stdin.on('end', () => resolve(Buffer.concat(chunks)));
    process.stdin.on('error', reject);
  });
}

async function cmdUpload(args) {
  const privKeyRaw = args.key;
  if (!privKeyRaw) fatal('--key is required');
  const evmRpc     = args.evm     || EVM_RPC;
  const indexerRpc = args.indexer || INDEXER_RPC;

  const privKey = privKeyRaw.startsWith('0x') ? privKeyRaw : `0x${privKeyRaw}`;

  const data = await readStdin();
  if (data.length === 0) fatal('no data on stdin');

  const provider = new ethers.JsonRpcProvider(evmRpc);
  const signer   = new ethers.Wallet(privKey, provider);
  const memData  = new MemData(data);
  const indexer  = new Indexer(indexerRpc);

  const uploadOpts = {
    expectedReplica: 1,
    skipTx: false,
    finalityRequired: false,  // don't wait for finality — just submission
  };

  let lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    process.stderr.write(`[zg-sidecar] upload attempt ${attempt}/3\n`);
    const [result, err] = await indexer.upload(memData, evmRpc, signer, uploadOpts);

    // ── Treat "already exists" as success ────────────────────────────────────
    const alreadyExists = err !== null &&
      (err.message?.toLowerCase().includes('already') ||
       err.message?.toLowerCase().includes('exist') ||
       err.message?.toLowerCase().includes('duplicate'));

    if ((err === null || alreadyExists) && result) {
      let rootHash, txHash;
      if ('rootHash' in result) {
        rootHash = result.rootHash;
        txHash   = result.txHash;
      } else {
        rootHash = result.rootHashes[0];
        txHash   = result.txHashes[0];
      }
      if (alreadyExists) {
        process.stderr.write(`[zg-sidecar] data already on 0G, returning existing root\n`);
      }
      process.stdout.write(JSON.stringify({ rootHash, txHash: txHash ?? null }) + '\n');
      return;
    }

    lastErr = err;
    if (attempt < 3) {
      process.stderr.write(`[zg-sidecar] attempt ${attempt} failed: ${err?.message} — retrying in ${3*attempt}s\n`);
      await new Promise(r => setTimeout(r, 3000 * attempt));
    }
  }
  fatal(`upload failed after 3 attempts: ${lastErr?.message}`);
}

async function cmdDownload(args) {
  const root = args.root;
  if (!root) fatal('--root is required');
  const indexerRpc = args.indexer || INDEXER_RPC;
  const maxAttempts = parseInt(args.retries ?? '8', 10);
  const baseDelay   = parseInt(args.delay   ?? '5000', 10);  // ms

  const rootHash = root.startsWith('0x') ? root : `0x${root}`;
  const tmpPath  = join(tmpdir(), `zg-dl-${Date.now()}-${Math.random().toString(36).slice(2)}.bin`);

  const indexer = new Indexer(indexerRpc);

  let lastErr = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    process.stderr.write(`[zg-sidecar] download attempt ${attempt}/${maxAttempts}\n`);

    const dlErr = await indexer.download(rootHash, tmpPath, false);

    if (dlErr === null) {
      const bytes = readFileSync(tmpPath);
      try { unlinkSync(tmpPath); } catch (_) {}
      process.stdout.write(bytes);
      return;
    }

    lastErr = dlErr;
    const msg = dlErr.message ?? String(dlErr);

    // Only retry on propagation-related errors
    const isNotFound = msg.toLowerCase().includes('no locations') ||
                       msg.toLowerCase().includes('0 locations') ||
                       msg.toLowerCase().includes('not found');

    if (!isNotFound) {
      // Hard failure — no point retrying
      fatal(`download failed: ${msg}`);
    }

    if (attempt < maxAttempts) {
      const wait = baseDelay * attempt;
      process.stderr.write(`[zg-sidecar] not yet propagated, retrying in ${wait/1000}s: ${msg}\n`);
      await new Promise(r => setTimeout(r, wait));
    }
  }

  fatal(`download failed after ${maxAttempts} attempts: ${lastErr?.message}`);
}

const [,, cmd, ...rest] = process.argv;
const args = parseArgs(rest);

if (cmd === 'upload') {
  cmdUpload(args).catch(e => fatal(e.message));
} else if (cmd === 'download') {
  cmdDownload(args).catch(e => fatal(e.message));
} else {
  fatal(`unknown command: ${cmd ?? '(none)'}. Use 'upload' or 'download'`);
}