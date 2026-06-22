"""ML ranking layer (Layer B) — LightGBM scoring + walk-forward training.

Sits above `strategy` (rules/exits/sizing) and `backtest` in the dependency
DAG: ranker* depend on both, but neither depends back on ranking (F-008).
"""
