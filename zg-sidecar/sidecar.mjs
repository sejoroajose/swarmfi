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
 *
 * The Python client calls this via subprocess, piping data through stdin/stdout.
 * No Python import namespace issues — this is a completely separate process.
 */

import { Indexer, MemData } from '@0gfoundation/0g-ts-sdk';
import { ethers } from 'ethers';
import { writeFileSync, readFileSync, unlinkSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

const EVM_RPC     = 'https://evmrpc-testnet.0g.ai';
const INDEXER_RPC = 'https://indexer-storage-testnet-turbo.0g.ai';

// ── arg parser ────────────────────────────────────────────────────────────────

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

// ── read stdin completely ─────────────────────────────────────────────────────

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on('data', c => chunks.push(c));
    process.stdin.on('end', () => resolve(Buffer.concat(chunks)));
    process.stdin.on('error', reject);
  });
}

// ── upload ────────────────────────────────────────────────────────────────────

async function cmdUpload(args) {
  const privKeyRaw = args.key;
  if (!privKeyRaw) fatal('--key is required');
  const evmRpc     = args.evm     || EVM_RPC;
  const indexerRpc = args.indexer || INDEXER_RPC;

  const privKey = privKeyRaw.startsWith('0x') ? privKeyRaw : `0x${privKeyRaw}`;

  // Read data from stdin
  const data = await readStdin();
  if (data.length === 0) fatal('no data on stdin');

  // Build ethers signer
  const provider = new ethers.JsonRpcProvider(evmRpc);
  const signer   = new ethers.Wallet(privKey, provider);

  // Wrap bytes in MemData (no temp file needed)
  const memData = new MemData(data);

  const indexer = new Indexer(indexerRpc);

  const uploadOpts = {
    expectedReplica: 1,
    skipTx: false,
    finalityRequired: false,  // faster for testnet
  };

  const [result, err] = await indexer.upload(memData, evmRpc, signer, uploadOpts);
  if (err !== null) fatal(`upload failed: ${err.message}`);

  // Result shape: {rootHash, txHash, txSeq} for single file
  // or {rootHashes[], txHashes[], txSeqs[]} for fragments (>4GB)
  let rootHash, txHash;
  if ('rootHash' in result) {
    rootHash = result.rootHash;
    txHash   = result.txHash;
  } else {
    // fragmented upload — return first root (SwarmFi files are small)
    rootHash = result.rootHashes[0];
    txHash   = result.txHashes[0];
  }

  process.stdout.write(JSON.stringify({ rootHash, txHash }) + '\n');
}

// ── download ──────────────────────────────────────────────────────────────────

async function cmdDownload(args) {
  const root       = args.root;
  if (!root) fatal('--root is required');
  const indexerRpc = args.indexer || INDEXER_RPC;

  const rootHash = root.startsWith('0x') ? root : `0x${root}`;

  // Download to a temp file then stream to stdout
  const tmpPath = join(tmpdir(), `zg-dl-${Date.now()}.bin`);

  const indexer = new Indexer(indexerRpc);
  const dlErr   = await indexer.download(rootHash, tmpPath, false);
  if (dlErr !== null) fatal(`download failed: ${dlErr.message}`);

  const bytes = readFileSync(tmpPath);
  try { unlinkSync(tmpPath); } catch (_) {}

  process.stdout.write(bytes);
}

// ── main ──────────────────────────────────────────────────────────────────────

const [,, cmd, ...rest] = process.argv;
const args = parseArgs(rest);

if (cmd === 'upload') {
  cmdUpload(args).catch(e => fatal(e.message));
} else if (cmd === 'download') {
  cmdDownload(args).catch(e => fatal(e.message));
} else {
  fatal(`unknown command: ${cmd}. Use 'upload' or 'download'`);
}
