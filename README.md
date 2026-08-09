# finance-agent — a cheat-proof research sandbox

**One sentence:** a harness that runs untrusted (AI- or human-written)
strategy code against journaled market data, where look-ahead bias is
impossible by construction and every run is byte-for-byte replayable.

**The problem it solves:** AI-generated quant research cannot be trusted.
Anyone can prompt a model into a beautiful Sharpe ratio; almost all such
results are garbage because the code peeked at the future or the run is
not reproducible. This is not fixable with better prompts — only with
infrastructure that makes cheating structurally impossible.

**The demo that is the whole pitch:** take an agent-written strategy with
a great backtest, prove in one command that it cheated, then show an
honest run of the same idea.

## Scope (and the fence around it)

Three components. If a fourth appears, scope has bled.

1. **Time-gated data API** — strategy code never touches files; it gets
   one object that serves data only up to the simulated "now."
2. **Sealed execution cell** — strategies run in an isolated subprocess:
   no network, no filesystem, resource limits, honest accounting.
3. **Reproducibility ledger** — every run is
   `(data hash, code hash, seed) → result`, re-runnable to identical
   bytes.

CLI: four verbs, no more — `sbx run`, `sbx verify`, `sbx ls`, `sbx seal`.

**Non-goals, permanently:** dashboards, strategy libraries, ML, broker
integration, live trading, web anything.

## Relationship to `finnce`

This repo consumes the journals `finnce` Phase 1 produces. Division of
labor: **finnce is hand-built by Nikayel** (the learning repo, no AI
implementation); **this repo is implemented by AI agents** against a
written build plan, with human review gates after every milestone and a
human-authored adversarial suite. The product's thesis — verify
AI-written code adversarially — is also its build process.

The planning documents (PRD, system design, roadmap, agent build plan)
are kept out of the published repo on purpose; `CHANGELOG.md` records
what each milestone actually delivered and what it deliberately did not.
