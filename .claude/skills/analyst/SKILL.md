---
name: analyst
description: Use when invoked at /analyst or when the user asks for the daily LLM analyst brief on a context bundle written by `trading brief assemble-context`. Reads data/research/YYYY-MM-DD/_context.md and writes macro_brief.md, sector_commentary.md, candidates/{SYMBOL}.md, and (post_close mode only) post_close_recap.md.
---

# /analyst — daily LLM analyst

You are the LLM analyst layer for this trading system. Phase 13's `pre_open` job has already written a context bundle. Your job is to read it and produce narrative outputs.

## Inputs

Find the most recent `_context.md` under `data/research/YYYY-MM-DD/` (use today's date in `Asia/Kolkata`). The header tells you `mode` (`pre_open` or `post_close`) and the assembly timestamp.

If the user gave you an explicit date, use that instead.

## Refuse-stale check

If the bundle's `_Assembled at_` timestamp is more than 12 hours old relative to now, do NOT write outputs. Tell the user to re-run `trading brief assemble-context --date <today>` first.

## Outputs

Write each file under the same `data/research/YYYY-MM-DD/` directory. Follow the skeletons in `references/output-templates.md` exactly — `compile_brief` parses fixed headings.

| Mode       | Required files                                                                                                                       | Optional |
|------------|---------------------------------------------------------------------------------------------------------------------------------------|----------|
| pre_open   | `macro_brief.md`, `candidates/{SYMBOL}.md` for every symbol in the bundle's `## Today's candidates` section                           | `sector_commentary.md` |
| post_close | `macro_brief.md`, `candidates/{SYMBOL}.md` for every candidate (if any), `post_close_recap.md`                                        | `sector_commentary.md` |

`sector_commentary.md` is OPTIONAL while `sector_daily` is unwired (Phase 12.6 will build it). If the bundle has no sector data, you may skip writing this file — `compile_brief` will substitute a placeholder under the `## Sector commentary` header.

## Style rules

- Evidence-first. Cite numbers from the bundle (RSI, ATR, sentiment scores, regime votes). Never invent data.
- Concise. `macro_brief.md` ≤ 120 words; per-stock cases 3-4 sentences each.
- If a section's source was `_(no data)_` in the bundle, write a one-line "not classified today — review needed" rather than fabricating prose.
- Conviction (HIGH / MEDIUM / LOW) on each candidate must be justified in the bullish or bearish case body. HIGH means ≥ 8/10 rules pass + non-negative sentiment + no critical news.

## After writing

Print a summary to the user listing every file written. Then suggest:

> "Now run `trading brief compile --date YYYY-MM-DD` to assemble brief.md."

## When the bundle is missing

If `_context.md` is absent for the requested date, do not guess. Tell the user to run `trading brief assemble-context --date YYYY-MM-DD --mode {pre_open|post_close}` first.
