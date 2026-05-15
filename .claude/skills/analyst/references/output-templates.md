# Output templates for /analyst

`compile_brief` parses fixed headings. Use these skeletons exactly.

## `macro_brief.md`

    Regime is **{RISK_ON|NEUTRAL|RISK_OFF}** ({score} / 4). {1-2 sentences citing
    VIX, FII flow, USDINR Δ from the bundle.} {1 sentence on what this implies
    for new positions today.}

If macro section was `_(no data)_`, write only:

    Macro: not classified today — review needed.

## `sector_commentary.md`

One block per active sector:

    ### {SECTOR_NAME} — {LEADING|NEUTRAL|LAGGING}
    - 5d relative strength: {value}%
    - Driver: {one-line explanation}

## `candidates/{SYMBOL}.md`

    # {SYMBOL} — Conviction: {HIGH|MEDIUM|LOW}

    ## Bullish case
    {3-4 sentences citing rule pass count, sector strength, sentiment score.}

    ## Bearish case / risks
    {3-4 sentences citing failed rules, drawdowns, negative news.}

    ## Event risks in 25-day horizon
    - {YYYY-MM-DD}: {event} — {impact note}
    - (or: "(none in horizon)" if nothing)

## `post_close_recap.md` (post_close mode only)

    ## Day's market
    {2-3 sentences on the day's price action, regime moves, kill-switch firings.}

    ## Prediction-error analysis
    {Commentary on the matured-predictions table from the bundle: average error,
    notable hits and misses, calibration drift.}

    ## Kill-switch notes
    - {Switch X}: fired / close-but-no-fire / quiet
