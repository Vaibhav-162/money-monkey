# Analysis package

Local scoring pipeline. Does **not** scrape the web.

CLI: `python run_analysis.py --out data`

Network jobs you run yourself:

- `python scripts/rescrape_gmp_history.py --out data --resume --workers 4 --delay 1.5`
- `python scripts/fetch_prices.py --spot-check` then `--out data --workers 4`

Interrupted GMP/price runs: `--merge` concatenates worker part files (GMP) or
rebuilds `returns.csv` from daily parquets (prices) without hitting the network.

## Layout

| Module | Role |
| --- | --- |
| `analysis/load.py` | `read_master`, drop FPO/REIT/InvIT, join GMP history + prices |
| `analysis/features.py` | Close-day features only. Allotment probability. Dual GMP %. |
| `analysis/impute.py` | Ratio medians for logistic only. Never impute GMP. |
| `analysis/targets.py` | `is_clean_pop`, open return, realized EV, 4-point quality checklist |
| `analysis/baselines.py` | Friend rules + live-feasible total/GMP/size rule + quality-checklist vs `exret_126` eval |
| `analysis/eda.py` | Per-board correlation, VIF, cohorts |
| `analysis/models.py` | LightGBM (classifier + open-return regressor) + logistic, **one model per board** |
| `analysis/tuning.py` | Small hyperparameter grid search via inner expanding-window CV, per board |
| `analysis/backtest.py` | Walk-forward by listing year; stop-loss sensitivity (hypothetical); pooled ablation is not the scorer |
| `analysis/score.py` | `score_features(row)` dispatches on `exchange_type`. S1 is APPLY/SKIP; S2 is the 4-point checklist |
| `analysis/prices.py` | Fetch helpers used by `scripts/fetch_prices.py`; load cache |

## Hyperparameters

`max_depth`, `learning_rate`, `min_child_weight`, `num_leaves` are picked per board by
`analysis/tuning.py` from a small grid, scored on inner expanding-window folds carved out of
each outer walk-forward fold's *own* training slice (never touching the outer test year). If a
board/window doesn't have enough inner folds to trust the search, it falls back to one fixed,
documented default rather than a hardcoded per-board rule.

## Strategy 1 EV sizing

`fit_s1` fits two heads: the `is_clean_pop` classifier (`p_pop`) and, when there are ≥40 rows
with a known `open_return_pct`, an `LGBMRegressor` predicting the continuous open-day return.
`score_features()` uses the regressor's prediction to size `ev_retail` (`ev_retail_source:
"lgbm_regressor"`). Only when too few rows exist to fit a regressor does it fall back to a
rough linear proxy in `p_pop` (`ev_retail_source: "p_pop_heuristic_fallback"`) — that fallback
is a stand-in, not a fitted expected-return model.

## Stop-loss sensitivity (hypothetical, not the backtest)

The primary Strategy 1 backtest (`realized_ev`) assumes you flip at the listing open, so an
intraday stop is moot there. `backtest.stop_loss_sensitivity()` answers a different, clearly
labeled hypothetical: "if I held through day 1 with a protective stop at issue×(1+stop%)
instead," using listing low/close, for stop% in {0, -5, -10}. Written to
`data/analysis/s1_stop_loss_sensitivity_hypothetical.json`; never fed into training or scoring.

## Leakage

Listing OHLC, `listing_day_gain_pct`, `current_price`, `profit_loss_pct` are labels only.

`gmp_at_close` is last GMP with date ≤ `ipo_close` when `data/gmp_history.csv` exists. Otherwise it copies `gmp_rs` and sets `gmp_anchor=listing_date_leaky`.

## Boards

Mainboard and SME never share fitted weights. `score_features` routes to `{board}_s1.pkl` / `{board}_s2.pkl`.

## Strategy 2 live output (hybrid)

`score_features()` Strategy 2 is always the 4-point quality checklist from
`quality_ranker()` / `quality_checklist_for_row()`:

- subscription > 20x
- OFS ratio < 50% (missing OFS still awards the point)
- ROE > 15% (raw, never imputed)
- debt-to-equity ≤ 0.5 (missing D/E still awards the point)

`s2_score` / `quality_score` is 0–4. `apply_s2` is true when that score is ≥
`QUALITY_PASS_THRESHOLD` (3). `quality_breakdown` lists each check as
`pass` / `fail` / `not_disclosed` so a missing OFS or D/E is not shown as a
genuine pass.

The price-alpha LightGBM regressor still trains and is walk-forward backtested
in `s2_walkforward.json` for monitoring. It is **not** what `apply_s2` uses.
When a fitted S2 bundle exists, its prediction is exposed only as
`s2_model_exret_pred` with `s2_model_status: "experimental_unvalidated"`.

`run_analysis.py` writes `data/analysis/s2_quality_checklist_eval.json`: per
board, buckets of `quality_score` 0–4 plus a pass/fail rollup against realized
`exret_126` / `s2_beat`. Rebuilt every analysis run.

That eval does **not** show a 6-month return edge at threshold 3 (Mainboard
pass group has a *lower* mean excess return than the fail group; SME is roughly
flat). Treat `apply_s2` as a **sanity checklist** that the business looks clean
enough to consider holding, not as a forecast that it will beat Nifty.

## Strategy 2 walk-forward metrics

Once `scripts/fetch_prices.py` has been run and `exret_126`/`ret_126` are populated,
`walk_forward_s2` reports hit ratio, mean excess return, top-30% mean excess return,
max drawdown, plus `information_ratio_proxy` (mean/std of excess return across a fold's
top-30% picks) and `cagr_proxy` (mean of each pick's own 126-session return annualized).
Both are cross-sectional proxies — one observation per IPO, not a per-period portfolio
return series — and are named `_proxy` so they aren't mistaken for a textbook time-series
Information Ratio or a compounded portfolio CAGR. These numbers describe the
research regressor, not the live `apply_s2` decision.

## Anchor lock-in features

`days_to_lockin_30`/`_90` are `anchor_lockin_30d`/`_90d` minus `listing_date`, in days —
real per-IPO values (often missing, since anchor lock-in dates aren't always published),
not the constant placeholders an earlier pass hardcoded.
