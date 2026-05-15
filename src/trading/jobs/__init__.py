"""Top-level jobs package — orchestrators that wire phases together."""

from trading.jobs.pre_open import PreOpenResult, run_pre_open

__all__ = ["PreOpenResult", "run_pre_open"]
