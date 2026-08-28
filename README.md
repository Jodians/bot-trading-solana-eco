# Solana Sniper (Paper / Dry-Run by default)

A Solana token sniping bot scaffold for newly launched pump.fun / Raydium-style
tokens. It polls for new tokens, runs on-chain safety + quality filters, and
(optionally) buys/sells via Jupiter.

> ⚠️ **RISK DISCLAIMER**
> Sniping is extremely high-risk. You can lose your entire stake to rug pulls,
> scam tokens, failed transactions, slippage, and volatile exits. This software
> is provided for educational/research purposes only. **No financial advice.**
> The authors are not responsible for any losses. Run at your own risk and only
> with funds you can afford to lose.

## Safety model

- **Default = PAPER MODE.** `LIVE_TRADING=false` in `.env`. No real transaction
  is ever sent. Buy/sell functions return fake "paper" results.
- Only when you explicitly set `LIVE_TRADING=true` (and fill real keys) does the
  bot attempt real swaps.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: fill HELIUS_API_KEY, WALLET_PRIVATE_KEY, etc.
```

Get a Helius API key at https://helius.xyz (free tier works for reads).
Generate a dedicated bot wallet — never reuse a wallet holding real funds.

## Run

```bash
python snipe.py
```

Watch the console. In paper mode you'll see tokens evaluated, PASS/SKIP, and
fake buy/sell results. Nothing is ever sent to the chain.

## Files

- `config.py` — loads `.env`, validates safety settings.
- `wallet.py` — loads keypair (live mode only).
- `filters.py` — on-chain authority/socials/mcap gates.
- `llm_analysis.py` — optional LLM quality gate via Conduit (Claude) before buy.
- `pumpfun_listener.py` — polls pump.fun new-token listing.
- `jupiter.py` — Jupiter Ultra buy/sell (paper-safe).
- `snipe.py` — orchestrator + main loop.

## Enabling live trading (DO THIS ONLY IF YOU KNOW THE RISKS)

1. Set `LIVE_TRADING=true` in `.env`.
2. Fill `WALLET_PRIVATE_KEY` with a **dedicated** wallet's base58 seed.
3. Fund that wallet with a small amount of SOL.
4. Tune `BUY_AMOUNT_SOL`, `TAKE_PROFIT_MULTIPLE`, `STOP_LOSS_MULTIPLE`.
5. Run `python snipe.py` and monitor closely.

## About "Claude Opus"

The original request asked to build this with Claude Opus. This scaffold was
written by the Hermes agent (model `tencent/hy3`). An optional **LLM pre-buy
analysis** step is wired in via `llm_analysis.py` using **Conduit**
(OpenAI-compatible endpoint at `conduit.ozdoev.net/v1`) — you can point it at a
Claude model (e.g. `anthropic/claude-3.5-sonnet`) by setting `LLM_MODEL`.

Enable it in `.env`:

```ini
LLM_ANALYSIS_ENABLED=true
CONDUIT_API_KEY=sk-cdt-...your key...
LLM_MODEL=anthropic/claude-3.5-sonnet
LLM_MIN_SCORE=60
```

Behavior: after on-chain filters PASS, the LLM scores the token 0-100. A BUY
only proceeds if verdict=BUY **and** score >= `LLM_MIN_SCORE`. On any API
error it FAILS SAFE to "PASS" (no buy), so a broken LLM step never lets a bad
token through. This analysis runs even in paper mode (it is not a chain action).

You can also run `claude-code` to have Anthropic's Claude Opus review or extend
this repo as a whole.

## Legal / ToS

Automated trading may violate platform Terms of Service and is subject to
local regulations. Ensure compliance in your jurisdiction before enabling
live trading.
