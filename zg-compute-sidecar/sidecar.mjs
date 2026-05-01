import { createZGComputeNetworkBroker } from '@0glabs/0g-serving-broker';
import { ethers } from 'ethers';
import OpenAI from 'openai';
import http from 'http';

const PROVIDER = '0xa48f01287233509FD694a22Bf840225062E67836';
const MODEL    = 'qwen/qwen-2.5-7b-instruct';
const EVM_RPC  = 'https://evmrpc-testnet.0g.ai';

let broker = null;

async function init() {
  const provider = new ethers.JsonRpcProvider(EVM_RPC);
  const wallet   = new ethers.Wallet(process.env.ZG_PRIVATE_KEY, provider);
  broker = await createZGComputeNetworkBroker(wallet);
  // One-time setup (idempotent if already done):
  try { await broker.ledger.addLedger(3); } catch (_) {}
  try { await broker.inference.acknowledgeProviderSigner(PROVIDER); } catch (_) {}
  try {
    await broker.ledger.transferFund(
      PROVIDER, 'inference', ethers.parseEther('1.0')
    );
  } catch (_) {}
  console.log('0G Compute sidecar ready');
}

const server = http.createServer(async (req, res) => {
  if (req.method !== 'POST' || req.url !== '/chat') {
    res.writeHead(404); res.end(); return;
  }
  let body = '';
  req.on('data', c => body += c);
  req.on('end', async () => {
    try {
      const { messages } = JSON.parse(body);
      const query = messages.at(-1).content;
      const { endpoint, model } = await broker.inference.getServiceMetadata(PROVIDER);
      const headers = await broker.inference.getRequestHeaders(PROVIDER, query);
      const openai  = new OpenAI({ baseURL: endpoint, apiKey: '' });
      const resp    = await openai.chat.completions.create(
        { messages, model },
        { headers }
      );
      await broker.inference.processResponse(
        PROVIDER,
        resp.id,
        resp.choices[0].message.content || ''
      );
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ content: resp.choices[0].message.content }));
    } catch (e) {
      res.writeHead(500);
      res.end(JSON.stringify({ error: e.message }));
    }
  });
});

await init();
server.listen(9099, () => console.log('listening on 9099'));