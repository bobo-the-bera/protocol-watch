# protocol-watch

A deliberately small, high-frequency archive for protocol documentation and public GitHub state.

## What it does

1. Reads each documentation site's published sitemap and snapshots **every discovered page** within the configured domain/prefix.
2. Keeps a few high-value direct URLs as fallbacks in case a sitemap is incomplete.
3. Normalizes rendered text only enough to remove known volatile metadata such as a relative `Last updated` counter.
4. Preserves visible wording **and link destinations**, so an href-only API/contract/documentation change is still archived.
5. On the direct `near.com` frontend target, also fingerprints script asset URLs as a deployment signal; this is kept out of the full docs crawl to avoid bundle-hash noise everywhere.
6. Records the current default-branch SHA of watched public repositories. GitHub's native commit history remains the code archive.
7. Commits only when normalized docs, sitemap membership, frontend deployment fingerprint, or repository heads actually change. Relevance filtering happens later, after the diff exists.

Git history is the archive: yesterday vs today is simply a commit diff.

## First run

The first successful run is a **baseline only**. Every page will appear as newly added because there is no earlier snapshot. Consumers must not interpret those additions as protocol changes.

## Zero-change days

If nothing changes, the workflow produces **no commit**. That is the clean zero-day signal. A scheduled analyst should not produce a long report for such a day.

## Timing

The GitHub Action runs **hourly at minute 17**. That gives high-frequency archival coverage without using ChatGPT to recrawl unchanged sites. The analysis layer should inspect only new archive commits since its previous check, then investigate the exact diff and current source context. GitHub schedules are best-effort rather than hard real-time, so this is an hourly monitor, not a sub-minute feed.

## Current coverage

- HyperLend docs: full published sitemap + direct Liquid Perps / UTA fallbacks
- Hyperliquid docs: full published sitemap
- NEAR Intents docs: full published sitemap
- near.com: full published sitemap, capped deliberately with a hard failure rather than silent truncation
- RHEA: full published sitemap
- HyperLend public repos: core, SDK, isolated, oracle, looping, audits
- NEAR public repos: Intents, MPC, Chain Signatures, Intents examples

## Run locally

```bash
pip install -r requirements.txt
python monitor/snapshot.py
pytest -q
```

To add a protocol or site, edit only `config/watch.yaml`.



