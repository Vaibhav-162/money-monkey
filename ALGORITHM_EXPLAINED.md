# How This Algorithm Actually Works — A Deep Walkthrough

This is the companion to [`FINAL_VERDICT.md`](FINAL_VERDICT.md). That file gives the verdict.
This file explains **how the pipeline was actually built**, step by step: what data exists,
what re-scraping added and why it was needed, which tools do which job, how each financial
number turns into a model input, and exactly why each strategy is (or isn't) trustworthy.

Written in plain English, but not simplified past the point of being wrong — every number
below comes from the real files in `data/`, not from memory.

---

## Part A — The datasets, and how they connect

Think of this as three layers stacked on top of each other. Each layer is a real file on disk,
and each one exists because the layer below it couldn't answer a specific question by itself.

### Layer 1 — The master sheet (`data/ipos.xlsx` / `data/ipos.csv`)

- **What it is:** One row per IPO, scraped from Chittorgarh.com. 1,944 rows, covering
  2016–2026, both Mainboard and SME.
- **How it was built:** `chittorgarh/pipeline.py` visits an index page per year/exchange
  (`tracker.py`, needs a real browser because that table is JavaScript-rendered), then visits
  each IPO's own detail page (plain HTML, no browser needed) and parses out ~140 fields:
  company identity, dates, price band, lot size, issue size, OFS split, subscription
  multiples, 3 years of financials (Assets/Income/PAT/EBITDA/Net worth/Borrowings), valuation
  ratios (P/E, P/B, ROE, ROCE, D/E), promoter shareholding, anchor investor details and their
  lock-in dates, and finally the listing-day OHLC prices.
- **Its GMP column, as originally scraped, is a problem** (see Part B — this is the #1 reason
  a re-scrape was needed).
- **Key column:** `ipo_id` — every other file below joins back to this master sheet using it.

### Layer 2 — Two re-scraped datasets that plug gaps the master sheet can't fill

**`data/gmp_history.csv`** (new; the GMP re-scrape)

- 3,463 rows, 1,440 unique `ipo_id`s (about 3 dated rows per IPO on average).
- Each row: `ipo_id, gmp_date, gmp_rs, gmp_pct, gmp_est_listing_price, kostak_rs, subject_to_sauda`.
- This is a **daily time series** of Grey Market Premium quotes, scraped per-IPO from
  InvestorGain's own GMP archive page (`chittorgarh/gmp.py`, `scripts/rescrape_gmp_history.py`).
- Unlike the master sheet's single GMP snapshot, this lets the analysis pick "whatever GMP was
  quoted on the exact date closest to (and not after) this IPO's subscription close date" —
  which is the only GMP value you could have actually known before the listing happened.

**`data/prices/` (`daily/*.parquet`, `nifty.parquet`, `returns.csv`)** (new; the price fetch)

- `daily/{ipo_id}.parquet`: daily OHLC price bars for 1,287 IPOs, fetched from Yahoo Finance
  (`yfinance`, primary) or NSE-direct (`jugaad-data`, fallback), for roughly 800 days starting a
  week before listing.
- `nifty.parquet`: the Nifty 50 index's own daily bars over the same span — the benchmark.
- `returns.csv`: **derived locally** (no network) from the two files above — for each IPO,
  its raw return and *excess return over Nifty* at 21/63/126/252 trading sessions after
  listing (roughly 1/3/6/12 months), plus a 126-session max drawdown and Sharpe ratio.
- This exists because the master sheet only has the listing-day price and a random "whatever
  the price was when we scraped it" snapshot — neither can tell you if an IPO was a good
  6-month hold.

### Layer 3 — What the analysis pipeline derives from Layers 1+2 (`data/analysis/`)

Everything here is 100% derived — delete it and `python run_analysis.py --out data` rebuilds
it from Layers 1 and 2 with no network calls:

| File | What it holds |
| --- | --- |
| `eda.json` | Correlations, VIF, cohort tables, distribution stats — per board |
| `baselines.json` | The two "friend rules" + a live-feasible rule + "apply to everything", scored |
| `s1_walkforward.json` / `s2_walkforward.json` | Year-by-year backtest results, per board |
| `s1_pooled_ablation.json` | "What if we didn't split Mainboard/SME?" sanity check |
| `s1_stop_loss_sensitivity_hypothetical.json` | "What if I held with a stop-loss instead of flipping at open?" |
| `excluded.csv` | The 35 rows dropped (FPOs/REITs/InvITs) and why |
| `predictions.csv` | Every row's model score, for audit |
| `models/{board}_{s1,s2}.pkl` | The 4 final fitted model bundles `score_features()` loads |
| `summary.json` | Everything above, rolled up |

**The join key everywhere is `ipo_id`.** Master sheet → GMP history joined on `ipo_id` +
date logic → prices joined on `ipo_id` → all of it flows into one wide in-memory table that
the model trains on. Nothing is joined on company name or ticker; that would be fragile because
tickers change and company names get typo'd across sources.

---

## Part B — Why the re-scraping was actually necessary

### B.1 — The GMP re-scrape: fixing a leakage bug, not adding a nice-to-have

The master sheet's original GMP field is **one snapshot taken on the day it was scraped**
(2026). For an IPO that listed in, say, 2022, that snapshot is a GMP quote from *years after*
the decision point you actually care about (3:30 PM on the IPO's close day, before listing).

Concretely: if you use that scrape-day snapshot as a feature, you are training the model on
information it could not possibly have had in real time — a classic case of **data leakage**.
A model trained this way can look great in a backtest and then fail live, because live it
won't have a time-traveled GMP number to cheat with.

The fix required an actual daily history, not a single number, so the pipeline could apply this
rule: *"use whatever GMP was quoted on the closest date on or before this IPO's close date."*
That rule lives in `chittorgarh/gmp.py`'s `last_gmp_on_or_before()` and is applied in
`analysis/load.py`'s `attach_gmp_at_close()`.

**What the re-scrape actually delivered**, from `data/analysis/eda.json` and `summary.json`:

| | Mainboard | SME |
| --- | --- | --- |
| Total rows | 526 | 1,383 |
| Got a genuinely leak-free GMP (`gmp_anchor=ipo_close`) | 18 | 29 |
| Fell back to the old, leaky snapshot (`gmp_anchor=listing_date_leaky`) | 303 | 694 |
| Got no GMP at all (`gmp_anchor=none`) | 205 | 660 |

So across both boards, only **47 of 1,909** modeled rows (≈2.5%) ended up leak-free. The other
997 with *some* GMP signal are still using the old, slightly-leaky snapshot — better than
nothing, but not clean. We confirmed directly on InvestorGain's own site that this isn't a
scraping bug: their browsable GMP archive genuinely has **no records at all for 2021–2023**
for most IPOs (verified even for a well-known 2022 Mainboard listing). The re-scrape got
everything that was technically retrievable; the ceiling on "what's retrievable" is just low
for older years.

### B.2 — The price fetch: creating Strategy 2's target from scratch

The master sheet has **no field anywhere for "price 6 months after listing."** Strategy 2
(long-term hold) is fundamentally a bet on what happens to the stock over the following
months — without real post-listing prices, there is nothing to train that model on or test it
against. `scripts/fetch_prices.py` had to be written and run specifically to create this.

**What it delivered:** out of 1,940 IPOs with a known listing date, **1,287 (66%)** got real
daily price data; from those, **1,286** have a full, computable 126-session (~6 month) return.
The other 34% are genuinely missing — mostly SME names that neither Yahoo Finance nor NSE-direct
(`jugaad-data`) carry (confirmed by a direct spot-check: 0 of 10 recent SME names checked had
any fetchable price history at all — see `data/prices/spot_check.json`).

### B.3 — Why parallelize both re-scrapes

Both re-scrapes are one Playwright-driven network request per IPO (GMP) or one API call per IPO
(prices) — with ~1,900 IPOs, doing this one at a time at a polite delay took the better part of
an hour and was the main practical bottleneck in this whole project. `chittorgarh/shards.py`
splits the IPO list round-robin across N worker processes; each worker opens its **own** browser
session (`chromium_session()` in `chittorgarh/browser.py`) once and reuses it for every IPO in
its shard (instead of relaunching Chromium per IPO), and writes to its own file
(`gmp_parts/shard_XX.csv` or its own set of `.parquet` files) so two workers never touch the same
file at once. The parent process waits for all workers, then merges the shard files into the
single `gmp_history.csv` / rebuilds `returns.csv`. This turned a ~40-60 minute serial job into a
few minutes with 4 workers, with no change to correctness — it's purely an engineering
optimization, not something that changes any number in the final analysis.

---

## Part C — Tools used, and what each one is actually doing

| Tool | Where | Job |
| --- | --- | --- |
| **Playwright** (headless Chromium) | `chittorgarh/tracker.py`, `chittorgarh/gmp.py` | Renders JavaScript tables (the yearly IPO index, and InvestorGain's GMP history table) that plain HTTP can't see |
| **httpx** (`chittorgarh/http.py`) | Detail-page fetches | Polite, delayed, disk-cached HTTP for static HTML — no browser needed here |
| **BeautifulSoup** (`chittorgarh/parse_ipo.py`) | Detail-page parsing | Pulls ~140 fields out of static HTML |
| **pandas / numpy** | Everywhere in `analysis/` | Cleaning, joining, feature math |
| **yfinance**, **jugaad-data** | `analysis/prices.py` | Post-listing daily stock prices (Yahoo primary, NSE-direct fallback) |
| **scikit-learn** | `analysis/models.py` | `LogisticRegression` (elastic-net penalty via `l1_ratio`) as a simple, interpretable baseline; `StandardScaler`; `brier_score_loss` for calibration scoring |
| **LightGBM** | `analysis/models.py` | The primary model — gradient-boosted decision trees, chosen because it handles missing values natively (important: GMP and fundamentals are genuinely, deliberately *not* imputed for this model) and copes well with a modest number of rows and mixed feature types |
| **SHAP** | `analysis/models.py` (`_shap_top`) | Explains *why* the model scored a given IPO the way it did, by attributing the prediction to individual features |
| **A hand-written mini hyperparameter search** | `analysis/tuning.py` | Picks `max_depth` / `learning_rate` / `min_child_weight` / `num_leaves` per board from a 36-combination grid, *without* peeking at the test year (explained in Part D) |

No neural networks anywhere — deliberately. With a few hundred to ~1,400 rows per board and
mostly tabular, mixed-type features, gradient-boosted trees are the standard, better-performing
choice over deep learning, and they come with native missing-value handling and SHAP support
that a neural net would need much more engineering (and much more data) to match.

---

## Part D — The pipeline, step by step (this is literally what `run_analysis.py` does)

1. **Load & sanitize** (`analysis/load.py`) — read the master CSV, strip `₹ $ % , x` characters
   from numeric-looking text columns, convert "None"/"NaN"/"" text to real missing values,
   parse all date columns. **Exclude** 35 rows whose name matches "FPO/REIT/InvIT Details" —
   these are not ordinary equity IPOs and don't belong in the model (`excluded.csv`).
2. **Flag, don't drop, outliers** (`flag_outliers`) — subscription >1000x, issue price >₹10,000
   (SME/REIT unit oddities), price outside the announced band, negative PAT. These are kept in
   the model with a flag column rather than silently deleted, because dropping them would bias
   the sample toward "normal-looking" IPOs and hide exactly the tail-risk cases a real investor
   would encounter.
3. **Attach GMP-at-close** (`attach_gmp_at_close`) — the leak-free join described in Part B.1.
4. **Attach post-listing returns** (`attach_prices`) — the join described in Part B.2.
5. **Feature engineering** (`analysis/features.py`, `add_features`) — turns raw columns into the
   ~20 model inputs listed in Part E. This is also where **allotment probability** (`p_allot`)
   is estimated and where ratio columns (ROE, ROCE, D/E, P/E, P/B) get a *dual* treatment: a
   `_raw` version (real value or missing — this is what LightGBM sees, since it handles missing
   natively) and a `_filled` version (median-imputed by industry+year, then by year, then
   globally — this is what the interpretable logistic-regression baseline sees, since it can't
   handle missing values at all). **GMP itself is never imputed**, in either version — a missing
   GMP stays missing, with a `gmp_missing` flag column so the model can learn "this IPO simply
   had no visible grey-market signal" as its own distinct signal.
6. **Target construction** (`analysis/targets.py`, `add_targets`) — builds `is_clean_pop`,
   `open_return_pct`, the Expected-Value fields, and the Strategy-2 targets. Explained fully in
   Part E and Part F.
7. **EDA** (`analysis/eda.py`) — per board: Pearson + Spearman correlation of 10 key features
   against listing-day gain, Variance Inflation Factor (checks whether features are redundant
   with each other), distribution shape (skew/kurtosis/5% VaR/95th percentile) of listing gains,
   and cohort tables (how gain and pop-rate vary across subscription tiers and issue-size tiers).
   This is what confirmed, before any model was even trained, that GMP% and subscription
   multiple are the two strongest raw signals — the model later "discovering" the same two
   features via SHAP is a good cross-check, not a coincidence.
8. **Baselines** (`analysis/baselines.py`) — mechanically scores the two rules your friends
   gave you, plus a "live-feasible" version that only uses fields a bot could legally/actually
   read (no paywalled QIB/Retail split, no intraday GMP trend), plus "apply to every 2020+ IPO"
   as the zero-skill control group everything else must be compared against.
9. **Hyperparameter tuning, nested inside training** (`analysis/tuning.py`) — before fitting the
   real model for a given walk-forward fold, the code carves the *training* data (never the test
   year) into its own **inner** expanding-window folds by year, tries all 36 combinations of
   `max_depth × learning_rate × min_child_weight × num_leaves` on those inner folds, and keeps
   whichever combo scored best (highest mean EV of its own top-20% picks, for the classifier;
   lowest RMSE, for the regressor). If there isn't at least one usable inner fold (common in
   early years when there's little history yet), it falls back to one fixed, documented default
   instead of guessing. This exists specifically so that Mainboard and SME are allowed to end up
   with *different* tree depths/leaf counts **because the data said so**, not because someone
   hardcoded "SME probably needs deeper trees."
10. **Model fitting** (`analysis/models.py`) — for Strategy 1: an `LGBMClassifier` on
    `is_clean_pop` (yes/no did it pop cleanly), plus, whenever ≥40 rows have a known
    `open_return_pct`, a second `LGBMRegressor` that predicts the *actual percentage gain*, not
    just yes/no — this second model is what lets the Expected-Value number be a real rupee
    estimate instead of a rough guess. A `LogisticRegression` is also fit as a simpler,
    fully-interpretable fallback, used only if there isn't enough data to trust LightGBM.
    For Strategy 2: one `LGBMRegressor` predicting 6-month excess return, with **monotonic
    constraints** — the model is mathematically forbidden from ever learning that higher ROE,
    higher ROCE, or higher PAT growth *hurts* the score, or that higher debt-to-equity, higher
    OFS ratio, or a richer relative valuation *helps* it. This bakes basic financial logic
    directly into the model's structure so it can't invent a nonsensical relationship from
    noise in a small sample.
11. **Walk-forward backtest** (`analysis/backtest.py`) — train on every year strictly before the
    test year, test on that one year, then slide forward (2021→2022→…→2026 for Strategy 1;
    2018→…→2026 for Strategy 2, since it needs 6 months to mature so recent years often get
    skipped for having too few matured rows). **Never random shuffling** — shuffling would let
    the model "see the future" relative to some of its training rows, which is meaningless for
    a tool meant to score IPOs that haven't happened yet.
12. **Threshold calibration** (`calibrate_threshold`) — for Strategy 1, instead of an arbitrary
    "50% probability = apply" cutoff, the code tries 13 thresholds from 0.2 to 0.8 *on that test
    year* and reports whichever one would have maximized mean EV — this is disclosed in the code
    and in `docs/codebase/analysis.md` as being *within* that test year, so it is a best-case
    calibration number, not a fully out-of-sample one. It's a reasonable way to show "how good
    could this be if you calibrated well," but it's slightly optimistic versus a strategy that
    picks one fixed threshold and never updates it.
13. **SHAP explanation** — for every fold, the top-5 features driving that fold's model, so you
    can see the model's reasoning evolve year to year, not just trust a black box.
14. **Final production models** — after all the backtest folds are done, one more model per
    board/strategy is fit on **all** available 2020+ data (not held back into folds) and saved
    as `data/analysis/models/{board}_{s1,s2}.pkl`. These, not any individual fold's model, are
    what `score_features()` actually loads and uses for a brand-new IPO.
15. **Scoring a live IPO** (`analysis/score.py`, `score_features()`) — takes a dict of an IPO's
    known-at-3:30pm-close-day fields, runs it through the same feature engineering as training,
    routes it to the correct board's model, and returns `p_pop`, `exp_open_return_pct`,
    `p_allot`, `ev_retail`, `apply_s1`, `s2_score`, `apply_s2`, and a `liquidity_flag`.

---

## Part E — Financial terms glossary: what they mean and how the algorithm actually uses them

| Term | What it means in plain English | Exactly how the algorithm uses it |
| --- | --- | --- |
| **GMP (Grey Market Premium)** | The informal, off-exchange price premium at which IPO shares are traded before they officially list — essentially an unregulated "what the street thinks this is worth" signal. | Converted to `gmp_pct_vs_issue` and `gmp_pct_vs_cap` (GMP as a % of issue price / of the price band's upper end). **The single strongest predictor of listing pop in both boards** (SHAP rank #1 Mainboard, #2 SME). Never imputed — if it's missing, a `gmp_missing` flag tells the model that explicitly instead of guessing a number. |
| **Total Subscription (`sub_total_x`)** | How many times over the total shares on offer were bid for, across every investor category combined. | The #2 SHAP driver everywhere. Also, mechanically, the denominator in the simplest allotment-probability estimate (`p_allot ≈ 1/sub_total_x`) — more demand means more upside *and* less chance you personally get shares. This tension is exactly why the Expected-Value framework exists. |
| **QIB / NII / Retail subscription splits** | How subscription breaks down by investor category (Qualified Institutional Buyers, Non-Institutional/HNI, Retail). | **Deliberately excluded.** Most rows in the raw data carry a `subscription_category_paywalled` warning — this breakdown is genuinely paywalled at the source for the majority of IPOs, so using it would mean training on data a live bot could not actually read. |
| **Allotment probability (`p_allot`)** | Your actual odds, as a retail applicant, of receiving any shares at all in an oversubscribed IPO. | Estimated three different ways depending on what's available for that IPO (best: retail lots ÷ total applications; fallback: 1 ÷ total subscription; special case: 100% if the issue was undersubscribed). Multiplies directly into `realized_ev` — this is the single biggest fix versus the original "friend rules," which never accounted for the fact that a huge pop is worthless if you never get allotted shares. |
| **Issue size (`issue_size_cr`)** | Total money being raised, in crores. | Used directly as a feature, and to build `size_lt_600` (small/mid-issue flag) and the SME `liquidity_risk` flag — smaller issues are easier for demand to overwhelm, which is part of why they pop harder but also carry more listing-day liquidity risk. |
| **OFS (Offer for Sale) Ratio** | What fraction of the issue is existing shareholders cashing out, versus the company raising fresh growth capital. | `ofs_ratio = ofs_cr / issue_size_cr`. Used as a feature, and forced **monotonically negative** in Strategy 2 (the model can never learn that more insider cash-out is a good sign for a long-term hold). |
| **P/E (Price-to-Earnings, `pe_pre`)** | How expensive the issue price is relative to the company's earnings. | Used directly, and also converted into `peer_rel_pe` — how this IPO's P/E compares to the median P/E of same-industry, same-year peers — to flag relative overpricing rather than judging P/E in a vacuum. |
| **P/B (Price-to-Book, `pbv`)** | Price relative to the company's net asset value. | A secondary valuation feature alongside P/E. |
| **ROE (Return on Equity)** | How efficiently the company turns shareholders' equity into profit. | Feature in both strategies; forced **monotonically positive** in Strategy 2 (higher ROE can never hurt the long-term score). |
| **ROCE (Return on Capital Employed)** | Similar to ROE, but relative to all capital employed (equity + debt), a broader efficiency measure. | Same treatment as ROE — monotonic-positive in Strategy 2. |
| **Debt-to-Equity (D/E)** | How leveraged the company is. | Monotonic-**negative** in Strategy 2 — more debt can never help the score. |
| **PAT CAGR** | 2-year compound annual growth rate of Profit After Tax, computed from the 3 years of financials on the master sheet. | Growth signal; monotonic-positive in Strategy 2. |
| **Promoter pre/post %** | How much of the company the founders/promoters hold before and after the IPO. | Feature in both strategies — a sharp drop after listing can signal promoters cashing out. |
| **Anchor investors & lock-in (30d/90d)** | Large institutions allotted a fixed share block a day before the IPO opens, legally locked in for 30 and then 90 days. | `days_to_lockin_30`/`_90` = the lock-in expiry date minus the listing date. A known real-world risk (informed institutional sellers dumping shares right after unlock) that the model can use if that date falls inside its ~6-month prediction window. |
| **Nifty 20-day return (`nifty_20d`)** | Whether the broad market was rallying or falling in the 20 trading sessions before this IPO's close day. | The only "market regime" signal usable at decision time without peeking into the future — a crude bull/bear-market flag. |
| **Listing-day open, high, low, close** | The stock's actual price action on its first trading day. | **Only ever used as a label/target, never as a feature.** Using it as an input would be circular — you can't know it before it happens. |
| **Lot size / retail min amount** | How many shares make up one "lot" you can apply for, and the minimum rupee amount that represents. | Used to turn a percentage gain into an actual expected rupee amount per lot (`expected_gain_amt`, `realized_ev`) — because "15% expected gain" means something very different on a ₹15,000 lot than on a ₹1,50,000 one. |
| **Liquidity risk / EV haircut** | A flag for small-issue SME IPOs with an unusually large GMP — historically prone to erratic, illiquid listing-day price action. | If flagged, the expected-gain estimate is multiplied by 0.7 (a 30% haircut) before computing EV, as a conservative adjustment for that extra risk. |

---

## Part F — Strategy 1 (listing-day pop): why it's usable, and why it isn't foolproof

### Why it's usable

- The two strongest features by SHAP — GMP% and total subscription — are exactly the two
  numbers markets and other IPO watchers already treat as the most important signals. The model
  didn't invent an exotic, unverifiable relationship; it correctly re-discovered the obvious one
  and weighted it well, and layered a *real* allotment-probability adjustment on top, which the
  original hand-written rules never had.
- On the SME board specifically, it beats "apply to everything" by roughly 2x on win rate
  (44% → 87%+ top-decile precision) with reasonably stable year-to-year numbers (±13 points).
- It only uses fields that are genuinely available at 3:30pm on close day — nothing paywalled,
  nothing that requires seeing the future.

### Why it isn't foolproof

1. **GMP, its #1 feature, is only leak-free for ~2.5% of the training history.** The model
   learned its most important relationship mostly from a slightly-time-shifted proxy, not the
   clean signal. (This should improve going forward, since live use has clean, real-time GMP —
   but that's a hope, not something the backtest can prove yet.)
2. **Mainboard results are noisy.** Precision@top-10% swung from 25% to 100% across different
   test years, because each year only has 38–104 Mainboard IPOs, and "top 10%" can be as few as
   4-9 companies — a couple of surprises swings the whole number by 25+ points.
3. **The EV threshold is calibrated on the same test year it's scored against** (a best-case,
   slightly optimistic number, clearly disclosed in the code and docs — not the same as a fully
   blind threshold picked in advance).
4. **Allotment probability is an approximation, not real data** — it's inferred from total
   subscription or from a lots/applications ratio, because the real category-wise breakdown is
   paywalled. If that approximation is systematically off for a particular kind of IPO, the EV
   number inherits that bias.
5. **`friend_rule1` (the strictest, most "sure thing"-looking rule) found only 25 Mainboard
   examples in 6 years.** A 100% historical hit rate on 25 cases is not the same statistical
   promise as a 100% hit rate on 2,500 cases — it's too small a sample to bet real, size-scaled
   money on blindly.
6. **The "live-feasible" rule and the trained model broadly agree**, which is reassuring, but
   both were built and tested on the *same* historical dataset — a genuinely new, unprecedented
   market regime (e.g. a regulatory clampdown on grey-market trading) could break the pattern in
   ways no amount of backtesting could have caught.

**Bottom line for Strategy 1:** good enough to use as a ranked shortlist and a rupee-EV
estimate, not good enough to apply blindly without glancing at the underlying numbers yourself.

---

## Part G — Strategy 2 (long-term hold): why it did not work

- On Mainboard, the model's own top 30% picks returned **+4.2%** excess return on average,
  while just holding a random basket of *every* Mainboard IPO returned **+7.9%** — the model's
  picks did *worse* than doing nothing. Its hit ratio (beating Nifty+5% over 6 months) was
  **43.5%**, below a coin flip.
- On SME, the headline number (`cagr_proxy` ≈127%) looks incredible, but its own error bar
  (±100%) is nearly as large as the number, meaning a handful of outlier IPOs are dragging the
  average — and the 2025/2026 test folds had so few matured price rows (2 and 0) they had to be
  **skipped entirely**, meaning the model was never actually validated on the recent period
  you'd care about most.
- SME's usable price sample (992 rows) is a **survivorship-biased subset**: Yahoo Finance
  essentially doesn't carry BSE SME tickers, and the NSE-direct fallback (`jugaad-data`) came
  back empty in a live 10-name spot-check. The SME names that *do* have fetchable prices are
  probably the larger, more established, longer-listed ones — not a random cross-section of the
  whole SME universe.
- The monotonic constraints (ROE/ROCE/growth can only help, debt/OFS/valuation can only hurt)
  are a reasonable prior from financial theory, but they are still an assumption imposed on the
  model, not something the data was free to disprove — if a genuine long-term SME winner in this
  dataset happened to carry high debt for a good reason (e.g. funding a plant that later paid
  off), the model was structurally forbidden from learning that nuance.

**Bottom line for Strategy 2:** the backtest is honest, and it says this doesn't work yet. The
blocker is data availability (SME prices, and IPOs too recent to have matured 6 months), not a
tuning problem you can fix by trying more hyperparameters.

---

## Part H — Concrete improvement points

1. **Start a live paper-trading log now.** Every time you run `score_features()` on a real
   upcoming IPO, save the prediction, and record the actual outcome a week/month/6-months later.
   This is the only way to build a genuinely leak-free dataset — historical GMP leakage is a
   ceiling that re-scraping cannot lift any further (InvestorGain's own archive is simply gone
   for 2021–2023).
2. **For Strategy 2, budget for a paid NSE/BSE market data subscription** if you want SME
   coverage to stop being the bottleneck — free sources have a real, structural gap here that no
   amount of extra scraping effort will close.
3. **Re-run `run_analysis.py` every few months.** As 2025/2026 SME IPOs cross the 6-month mark,
   the currently-skipped test folds will finally have enough rows to be evaluated — right now
   Strategy 2's SME numbers have never been tested on the period that matters most.
4. **Consider blending, not just falling back to, the logistic-regression baseline.** Right now
   logistic regression is only used when LightGBM can't be fit (too little data) — it's a
   backup, not a second opinion. A simple average of the two models' probabilities, or at least
   surfacing cases where they strongly disagree, could add a useful sanity check at very little
   extra cost.
5. **Track calibration over time.** Once live GMP data starts accumulating cleanly, periodically
   check whether the model's predicted probabilities still match observed outcomes (a rolling
   Brier score) — if live-GMP-driven scores drift away from the historical calibration, that's
   an early warning the backtest's leaky-GMP training has aged out of relevance.
6. **Widen the inner hyperparameter grid modestly** (e.g. add `n_estimators` and a light L2
   `reg_lambda` term to the search in `analysis/tuning.py`) once more years of clean data exist
   — right now the grid is intentionally small because there usually aren't enough inner folds
   to trust a bigger search, but that constraint eases as the dataset grows year over year.
7. **Add simple uncertainty bands to reported metrics**, given how few IPOs land in some yearly
   folds (as few as 38) — even a basic bootstrap confidence interval on `precision_top10` and
   `apply_mean_ev` would make it much easier to tell a real trend from small-sample noise at a
   glance, instead of having to reason about it manually every time (as this document had to).

---

## Part I — From backtest to live: how `score_features()` actually gets used on a real, upcoming IPO

Part D step 15 is not a sketch of a future API. It is the production call.

`analysis/score.py`'s `score_features()` is the same function the historical trainer
documented in Part D: take a dict of fields knowable at ~3:30 PM IST on close day, run it
through `analysis/features.py`'s `add_features()` (the same math as training), route it to
the correct board's model, and return `p_pop`, `exp_open_return_pct`, `p_allot`, `ev_retail`,
`apply_s1`, `s2_score` / `quality_score`, `apply_s2`, and a `liquidity_flag`. The production
caller is `scripts/live_scanner.py`'s `_score_one()`, which first maps the live scrape through
`analysis/live_audit.py`'s `to_score_row()` so the trainer's feature names are filled
(`gmp_rs` from today's InvestorGain scrape is copied onto `gmp_at_close` when that field is
empty; `listing_year` is inferred from the close date if the detail page didn't supply one).

The pickles it loads are the same four files Part D step 14 wrote:

`data/analysis/models/{board}_{s1,s2}.pkl` for `board` in `{mainboard, sme}`.

`run_analysis.py` does **not** import `analysis/score.py`. The trainer writes those bundles;
the live scanner later loads them via `analysis/models.py`'s `load_bundle()`. Nothing about
the model changes between backtest and live — not the feature list, not the board split, not
the apply threshold copied onto the S1 bundle. What changes is everything *around* the call:

| | Historical (`run_analysis.py`) | Live (`scripts/live_scanner.py`) |
| --- | --- | --- |
| Who is scored | Every modeled row in `data/ipos.csv` | IPOs whose `close_date` equals today IST |
| GMP | `attach_gmp_at_close()` against `data/gmp_history.csv` (Part B.1) | A same-day InvestorGain scrape (`chittorgarh/pipeline.py`'s `scrape_one`, Playwright) |
| Subscription | Whatever the master-sheet scrape stored | Live Chittorgarh `/ipo_subscription/{slug}/{id}/` via `chittorgarh/live_subscription.py`'s `fetch_live_subscription()` (`use_cache=False`) |
| After the score | Walk-forward metrics, SHAP, JSON summaries | Telegram/email cards, then one row in `data/live_audit_log.csv` |
| Listing OHLC | Used as a **label** (`analysis/targets.py`) | Must not be an input — it hasn't happened yet. Filled later by `scripts/verify_outcomes.py` |

Two live-only overlays sit *after* `score_features()` and do not go into the LightGBM
feature vector. They are worth naming here because Part E already defined the terms; this
is just where they get used:

- **`market_regime`** (`BULLISH` / `BEARISH` / `NEUTRAL`) — stamped by
  `analysis/market_regime.py`'s `fetch_market_regime()` from a 5-session Nifty-50 move
  (latest close vs five sessions earlier; ≤ −1.5% → `BEARISH`, otherwise `BULLISH`, too few
  points → `NEUTRAL`). Cached at `data/analysis/market_regime.json` for 6 hours; a Yahoo
  failure reuses a still-fresh cache or falls back to `NEUTRAL`. This is **not** the
  `nifty_20d` feature from Part E. It only changes Strategy 2 *card copy* when the quality
  checklist score is exactly 2 (`scripts/notify.py`'s `_s2_status_and_copy()`).
- **Same-day ranking** — `analysis/live_audit.py`'s `rank_same_day_candidates()` ranks
  `apply_s1` names that share a `close_date` by **`ev_capital_ratio`**
  (`ev_retail / (price_band_high * lot_size)`, falling back to `issue_price` if the band
  cap is missing), with live **`sub_qib_x`** as the tie-breaker. A lone applicant is left
  unranked (no "1 of 1" banner). Missing ratio or QIB sorts as worst, not as zero. Ranking
  never flips `apply_s1` / `apply_s2`. QIB remains what Part E said it is: deliberately
  excluded from the model; live uses it only as a display/ranking overlay.

Live Strategy 2 `apply_s2` is also worth stating plainly, because it is easy to misread
from the pickle filenames. `score_features()` **always** sets `apply_s2` from the 4-point
quality checklist (`quality_score` ≥ `QUALITY_PASS_THRESHOLD`, currently 3 in
`analysis/targets.py`). The S2 LightGBM, when the pickle exists, is exposed only as
`s2_model_exret_pred` with status `experimental_unvalidated`. A high S2-regressor number
cannot flip `apply_s2`. That matches Part G: the long-term model is not trusted enough to
drive a live apply/skip.

One operational constraint the backtest never had: GitHub Actions checks out a fresh clone
with no local training artifacts. The four `.pkl` files **must be committed**. If they are
missing, `Scorer._bundle()` returns `None`, `apply_s1` stays `False`, and `p_pop` is
`None` — every card silently degrades to Strategy 1 SKIP with no probability. That is not
a model decision; it is a missing-file failure that looks like one.

---

## Part J — The three production workflows, end to end

There is no always-on server. Three GitHub Actions workflows on `ubuntu-latest` are the
entire production runtime. They share `permissions: contents: write`, check out with
`fetch-depth: 0` (so a later `git pull --rebase` is not fighting a shallow clone), and
commit their artifacts back to the default branch with `GITHUB_TOKEN`.

### J.0 — What actually fires them (and what no longer does)

All three workflows are triggered the same way:

| Trigger | Role |
| --- | --- |
| External `repository_dispatch` POST from cron-job.org (fine-grained PAT, Contents read/write on this repo only — never committed) | **The only automatic clock.** HTTP 204 means GitHub accepted the webhook; Actions should then show event `repository_dispatch`. The daily scanner's documented event type is `trigger-daily-ipo-alert`. |
| `workflow_dispatch` ("Run workflow" in the Actions UI, or `gh workflow run`) | **Manual-only fallback.** For the two alerting workflows, the `dry_run` checkbox defaults to **true** so an off-hours test cannot consume the day's one real alert slot. Uncheck it (or `-f dry_run=false`) when you actually need a live send. A `repository_dispatch` run does **not** read `inputs.dry_run` — the step expression in both YAML files only appends `--dry-run` when `github.event_name == 'workflow_dispatch' && inputs.dry_run`. |

There is **no** internal GitHub `schedule:` (cron) trigger any more. Public-repo
`schedule` events were the previous automatic clock, and they were unreliable: multi-hour
delays (especially right at the top of an hour) and dropped ticks with no retry. That is
a documented GitHub platform behavior, not a timezone bug in this repo. Removing it as a
backup is a real trade, not an upgrade: you go from "unreliable-but-redundant" (an
external POST plus three delayed GitHub ticks that sometimes still landed) to
"cleaner-but-single-point-of-failure on cron-job.org." If that POST is missed, nothing
else in GitHub will fire the job. Someone has to notice and click Run workflow on `main`
with `dry_run` unchecked — and for the close-day scan, a next-day retry is too late to
bid (typical broker UPI cutoffs are 4:00–4:30 PM IST).

The scripts themselves have no clock. `chittorgarh/live_dashboard.py`'s `today_ist()` is
the calendar. cron-job.org is responsible for POSTing at the IST times the rest of the
system is written around:

| Intended IST window | Workflow | Why that window |
| --- | --- | --- |
| Close-day afternoon (documented target: 15:30 IST, while the bidding window is still open) | `daily_ipo_alert.yml` | QIB/HNI books are mostly in; you still have time before typical 4:00–4:30 PM IST UPI cutoffs. Cards are a **live snapshot** — GMP and subscription can still move. |
| Next weekday morning (documented target: 09:45 IST) | `verify_outcomes.yml` | Listing OHLC is often on the Chittorgarh detail page by then; if it isn't, the row stays unverified and this job retries the next weekday. |
| Weekday midday, 1–4 IST days after close (documented target: 12:00 IST) | `check_allotment.yml` | Registrars typically publish Basis of Allotment in this window; the script re-checks daily until it sees "out" or the window expires. |

Those times used to be GitHub `schedule` crons (the close-day workflow even had *three*
weekday ticks — 15:15 hedge / 15:30 primary / 16:00 catch-up — specifically because
GitHub cron slipped). They are now whatever times the external service is configured to
POST. The presence-only gate in Part K still means that even if cron-job.org *and* a
manual click both ran the same day, you get one close-day alert per `(ipo_id, close_date)`,
not two.

### J.1 — Calendar of one IPO through the three jobs

```
close day (IST)     afternoon   daily_ipo_alert  →  score + Telegram/email + audit row
close+1 .. listing  morning     verify_outcomes  →  fill actuals once OHLC is up
close+1 .. close+4  midday      check_allotment  →  "allotment out?" then PAN email / generic card
```

The join key across all three is the same row in `data/live_audit_log.csv`, keyed on
`(ipo_id, close_date)`. The close-day scanner *creates* the row (after a successful send).
Verify *fills* `actual_*` / `verified`. Allotment-check *flips* `allotment_notified`.
None of the three re-trains a model.

### J.2 — `daily_ipo_alert.yml` → `scripts/live_scanner.py`

**Job `scan`.** Python 3.11, `pip install -r requirements.txt`, then
`python -m playwright install --with-deps chromium` (GMP still needs a browser). Secrets
injected as env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GMAIL_USER`,
`GMAIL_APP_PASSWORD` (and legacy `GMAIL_PASS`), `ALERT_EMAIL_TO`. Runs
`python scripts/live_scanner.py --out data`, plus `--dry-run` only on a checked
`workflow_dispatch`.

`run_scan()` then does this, in order:

1. **Discover.** `chittorgarh/live_dashboard.py`'s `scrape_all_open_ipos()` fetches the
   Chittorgarh mainboard (`/ipo/`) and SME (`/ipo/ipo_dashboard.asp?a=sme`) dashboards as
   static HTML (`HttpClient`, `use_cache=False`, no Playwright). Date badges have no year;
   year is inferred from `as_of` (today IST).
2. **Empty-dashboard watchdog.** Zero rows on **both** boards is treated as a likely HTML
   structure break: `send_failure_alert()` fires (Telegram + email, capped at one send per
   IST day via `data/live_alert_state.json`). Zero *closers today* (IPOs exist, none close
   today) is a silent no-op — that is a normal day.
3. **Select closers.** `_select_candidates()` keeps rows with `close_date == as_of`
   (`closing_on()`). `--include-open` is a local/weekend testing flag; production does not
   pass it.
4. **Scrape + score each closer** (`_score_one()`). Detail page + optional InvestorGain GMP
   via `chittorgarh/pipeline.py`'s `scrape_one(..., use_cache=False)` on a reused Chromium
   page; then live subscription. A scrape or score exception becomes a `SCAN ERROR` record
   (`build_alert_record(..., error=...)`), never an ordinary SKIP — `scripts/notify.py`'s
   `format_card()` will say so explicitly so a failed fetch is not mistaken for a model
   decision. Missing GMP is rendered `GMP: not available`, never a fabricated 0.
5. **Stamp overlays.** `fetch_market_regime()` and `scrape_timestamps()` (UTC ISO + a
   locale-stable `DD-Mon HH:MM IST` stamp) on every record; then `rank_same_day_candidates()`.
6. **Presence-only gate.** `records_needing_alert()` drops any record whose
   `(ipo_id, close_date)` already exists in `data/live_audit_log.csv`. See Part K.
7. **`dispatch()` before `upsert_audit()`.** One Telegram card per IPO plus one HTML email
   digest (`scripts/notify.py`). If delivery raises `NotificationDeliveryError`, the audit
   write is skipped. Then, on a real (non-dry) run, `upsert_audit()` writes/refreshes the
   day's rows. Dry-run prints cards and writes nothing — a test must not create the "already
   alerted" row.
8. **Crash path.** `main()` catches any exception, calls `send_failure_alert()` with the
   traceback, then re-raises so Actions goes red. Failure-alert send exceptions are printed,
   not re-raised — a dead Telegram token must not hide the original traceback.

**What it commits** (step `Commit audit log`, `if: always()` so this still runs after a
red scan — that is how `live_alert_state.json` gets persisted even when the scanner
crashed):

- `data/live_audit_log.csv` if the file exists
- `data/live_alert_state.json` if the file exists
- File-existence guards, then `git commit` / `git pull --rebase` / `git push`. A day with
  zero closing IPOs (CSV untouched) exits 0 with `no audit changes`.

### J.3 — `check_allotment.yml` → `scripts/check_allotment.py`

**Job `allotment`.** Same Python/Playwright install, plus `sudo apt-get install -y tesseract-ocr`
because registrar portals put a captcha in front of the PAN lookup. Secrets: the Telegram
and Gmail set, plus `PAN_PROFILES` (JSON array of `{label, pan, email}` — GitHub secret,
never a repo file). Runs `python scripts/check_allotment.py --out data`, with the same
`--dry-run` / `workflow_dispatch` rule as the close-day job.

Unlike the close-day alert (one shot per `(ipo_id, close_date)` on the close date itself),
this **re-checks daily** for IPOs already in the audit CSV. `run_check()`:

1. Read `data/live_audit_log.csv`. Empty log → print and return.
2. For each row, `is_allotment_due()`: skip if `allotment_notified` is already true, or if
   `close_date` is missing, or if the IST age is outside **1–4 days** after close.
3. `inspect_row()` live-fetches the detail URL (`use_cache=False`), refreshes `registrar` /
   `allotment_date` if they were empty, and asks `allotment_is_out()`:
   - **Primary:** `chittorgarh/parse_ipo.py`'s `parse_allotment_published()` — a "Basis of
     Allotment" heading/link, or body text matching allotment out/finalized/published.
   - **Fallback:** timetable `allotment_date` has arrived (`as_of >= expected`). The date
     can slip a day; the heading is the stronger signal.
   Detection sets a transient `_just_notified` marker. It does **not** yet persist
   `allotment_notified` — that flag gates all future re-checks, so it is written only after
   dispatch returns (same ordering idea as the close-day scanner).
4. **Dispatch, then flag.**
   - `PAN_PROFILES` unset → `dispatch_allotment()`: one generic "allotment out" Telegram
     card (`format_allotment_card`) plus a digest email. Raises `NotificationDeliveryError`
     only when **both** real channels threw. Unconfigured channels stay a silent no-op.
   - `PAN_PROFILES` set → `dispatch_pan_results()`: Playwright + Tesseract against the
     registrar named on the row, via `chittorgarh/registrar_allotment.py`.
5. Mark `allotment_notified` + `allotment_notified_at` only if dispatch did not raise.
   Then `write_audit()`. Dry-run skips the write.

**PAN lookup, specifically** (`chittorgarh/registrar_allotment.py`):

- Automated checkers exist for **KFintech** (`kfin` / legacy `karvy`) and **MUFG Intime /
  Link Intime**. Bigshare, Cameo, Skyline, and Purva are recognized by name but
  `checker_for_registrar()` returns `None` — the caller then has no per-PAN result and
  those profiles are skipped (generic "allotment out" is the unset-`PAN_PROFILES` path;
  with profiles set and an unsupported registrar, everyone lands in the skip bucket).
- `check_kfintech` / `check_mufg` wrap a single page fill in `_with_retries()`: up to
  `CAPTCHA_ATTEMPTS` (4) retries on `captcha_failed` or a Playwright timeout. A
  selector/page-structure exception is recorded as `lookup_failed` and is **not** retried
  as if it were an OCR miss — that distinction exists so a systematically broken portal
  does not look like a normal one-off captcha failure.
- `parse_result_blob()` maps visible page text to `allotted` / `not_allotted` /
  `no_application` / `captcha_failed` / `lookup_failed`. Unmatched company dropdown →
  `company_not_found`.
- **Only `allotted` and `not_allotted` generate a personalized email** (`EMAIL_STATUSES`
  in `check_allotment.py`). `no_application`, `captcha_failed`, `lookup_failed`, and
  `company_not_found` stay silent — no email, and Telegram is skipped too if nobody in
  that batch was emailed. A detected "allotment out" still marks the row notified after
  a non-raising dispatch so the job does not retry that IPO forever.
- Telegram, when anyone *was* emailed, is counts only (`format_allotment_telegram_summary`:
  "N emailed, M skipped"). A Telegram failure there is printed, not raised — email already
  went out.
- Full PANs never go to git, logs, Telegram, the audit CSV, or a shared digest. Logs use
  `mask_pan()` (`ABCDE1234F` → `ABCDE***4F`). Personalized emails never include a PAN.
- `raise_if_systematic_lookup_failure()` / `assess_lookup_batch()`: a single captcha miss
  is normal. Every attempted lookup in a batch of ≥2 failing (`captcha_failed` /
  `lookup_failed` / `company_not_found`, zero resolved answers) raises
  `RegistrarLookupBatchError` — page structure or OCR is likely broken. Empty input does
  not escalate (nothing was attempted). `no_application` counts as a real registrar
  answer, not a failure.

If every profile that reached `EMAIL_STATUSES` then threw on `send_email`,
`dispatch_pan_results()` raises `NotificationDeliveryError` and `allotment_notified` is
never persisted, so the next run can retry.

**What it commits** (`Commit audit log`; **no** `if: always()`, so a raised delivery /
lookup error skips the commit — which is what you want, because the in-memory
`allotment_notified` flag never reached disk): `data/live_audit_log.csv` if it exists.

### J.4 — `verify_outcomes.yml` → `scripts/verify_outcomes.py`

**Job `verify`.** Python 3.11, `pip install -r requirements.txt` only — no Playwright, no
Tesseract, no Telegram/Gmail secrets. This job does not send alerts and has no `dry_run`
input. Runs `python scripts/verify_outcomes.py --out data`.

`run_verify()` walks the audit CSV:

1. Already-`verified` rows are left untouched (idempotent).
2. `verify_row()` re-fetches the same Chittorgarh detail URL (`use_cache=False`), parses
   with `parse_ipo_html` + `flatten_into_master`, then `analysis/live_audit.py`'s
   `compute_actuals(master)`.
3. `compute_actuals()` returns **`None` until listing open is published** (`listing_nse_open`
   / `listing_bse_open` / `list_open`, plus a usable `issue_price`). The row stays
   unverified and the next weekday's run tries again. That is the retry loop — there is
   no separate "keep trying" flag.
4. Once open is present, it writes `actual_listing_open`, `actual_open_return_pct`,
   `actual_is_clean_pop`, flips `verified` / `verified_at`, and fills
   `listing_date_expected` from the page if the audit row didn't have it.
5. Always rewrites `data/analysis/live_performance.json` via `performance_summary()` —
   even when the audit is empty — so the committed JSON cannot go stale. The snapshot is
   precision / predicted-vs-realized-EV-proxy over verified `apply_s1` rows, plus a
   listing-day open-return mean for quality-score ≥ 3. The JSON's own `note` field states
   that S2 listing-day return is **not** the 6-month target.

**Why `actual_is_clean_pop` uses listing OPEN, not the historical close-day-tracker gain.**

`analysis/targets.py`'s training label `is_clean_pop` is `(listing_day_gain_pct >= 15) AND
(listing low held above issue price)`. `listing_day_gain_pct` is only populated when a
`tracker` dict is passed into `parse_ipo_html()`. A live re-fetch of the **bare detail URL**
never has that tracker context, so anchoring the live outcome to `listing_day_gain_pct`
would leave every row's outcome `None` forever — while still being able to flip
`verified=True` if the code weren't careful, which is a silent "we checked, result
unknown" lock. `compute_actuals()` therefore defines a clean pop from the **open-price
return** (≥ 15% and the day's low held above issue, or low missing). That is also the
definition that matches the EV framework's "exit at listing open" assumption
(`open_return_pct` in Part E / Part F), so the live scorecard is scoring the thing the
rupee-EV number was actually estimating — not a different close-day tracker print the
re-fetch cannot see.

This is a real discrepancy with the historical target, not a rounding detail. The live
paper-trade log and the walk-forward `is_clean_pop` column are cousin labels, not the
same column rebuilt. Read `data/analysis/live_performance.json` as a forward-test against
open-price pops, and the backtest numbers in Part F as tracker-gain pops.

**What it commits:** `data/live_audit_log.csv` and `data/analysis/live_performance.json`,
with the same existence-guard / rebase / push pattern. No `if: always()`.

---

## Part K — Every decision point in the live system

### K.1 — The presence-only audit gate

`analysis/live_audit.py`'s `records_needing_alert(records, existing)` keeps a record only
when no `(ipo_id, close_date)` row exists yet in `data/live_audit_log.csv`. It does **not**
compare GMP, subscription, `apply_s1`, or any other field. The first real write of the day
is the one alert; later runs that day stay silent for that key even if the numbers moved.

That is why:

- Production used to fire three GitHub ticks plus an external POST on close day, and why
  a manual retry the same afternoon is safe — they cannot send three emails for one IPO.
- `--dry-run` / the Actions `dry_run` checkbox **must not write the audit**. A test write
  would suppress the real send for the rest of the day. `workflow_dispatch` defaults the
  checkbox to true specifically because an off-hours click (e.g. just after midnight IST)
  would otherwise burn the slot hours before the bidding window even opened.
- `upsert_audit()` refreshes scores for *all* of today's scored IPOs (so a later tick can
  update GMP/Sub on disk) but still preserves `verified*` and `allotment_notified*` if a
  later write hits the same key. The *alert* is presence-only; the *row* is upserted.

### K.2 — The canonicalized `_audit_key` fix

CSV round-trips and pandas dtype inference used to make the same IPO look like two keys:
int `2013` vs string `'2013'` vs `'2013.0'`, and a `date` vs `'YYYY-MM-DD'` vs
`'YYYY-MM-DDTHH:MM:SS'`. A mismatch in either direction is a production bug: duplicate
keys → duplicate alerts; a failed match → a missed catch-up that should have been
suppressed, or a suppressed retry that should have sent.

`_canonical_ipo_id()` / `_canonical_close_date()` / `_audit_key()` force a stable form
(`'2013|YYYY-MM-DD'`) before `records_needing_alert`, `upsert_audit`, and
`build_alert_record` compare or write. `upsert_audit()` also `drop_duplicates` on that
key defensively — a hand-edited CSV with two rows for the same key would otherwise make
`prior.loc[key]` return a DataFrame and crash the scalar merge.

### K.3 — Send first, then mark seen

Both alerting scripts write the "we already handled this" bit **after** the send:

| Script | Send | Persist-seen |
| --- | --- | --- |
| `live_scanner.py` | `dispatch(to_alert)` | then `upsert_audit()` |
| `check_allotment.py` | `dispatch_pan_results()` / `dispatch_allotment()` | then `allotment_notified = True` (and `write_audit()` after the loop) |

If `dispatch()` / allotment dispatch raises `NotificationDeliveryError`, the seen-bit is
never written. A credentials fix and retry can then actually send, instead of the
presence-only gate treating a lost alert as "already alerted" for the rest of the day
(close-day) or the rest of the 1–4 day window (allotment). The close-day comment in
`run_scan()` is explicit: writing first would permanently lose that IPO's alert for the
day even after a fix.

`upsert_audit()` on a later successful tick still updates the scored fields for keys that
*did* get written; it will not re-alert them. The ordering only matters for the first
write.

### K.4 — `NotificationDeliveryError`: a lost send is a red workflow, not a green no-op

`scripts/notify.py`'s `NotificationDeliveryError` is raised by `dispatch()` when a real
(non-dry) batch has records and **both** channels genuinely failed (an exception was
raised). The same class is raised by `check_allotment.py`'s `dispatch_allotment()` /
`dispatch_pan_results()` on the equivalent "every attempted real channel threw" case.

A channel that is simply **unconfigured** (`TELEGRAM_BOT_TOKEN` / `GMAIL_USER` unset)
returns `False` and is an intentional silent no-op — you can run Telegram-only or
email-only. That is not a failure. The raise exists so a dead Gmail app password or a
bad Telegram token becomes a red Actions run instead of a silently-green run that
delivered nothing. `send_telegram` / `send_email` themselves `raise_for_status` /
raise on SMTP errors; `_redact()` strips a bot token out of any exception string before
it hits logs.

`send_failure_alert()` (scanner crash / empty-dashboard) is deliberately *not* this
strict: its own send exceptions are printed, not re-raised, and it dedups to one attempt
per IST calendar day via `data/live_alert_state.json`. A failure-alert must not hide the
original traceback, and three retried crashes the same afternoon must not produce three
"FAILED" cards.

### K.5 — Decision points, end to end

Every branch the live system actually takes. "Silent" means no Telegram/email for that
IPO/event; the workflow can still go green.

| # | Decision | Where | If yes | If no |
| --- | --- | --- | --- | --- |
| 1 | Did cron-job.org POST `repository_dispatch` today? | External clock → workflow `on:` | Job runs for real (`--dry-run` not applied) | **Nothing runs.** No GitHub `schedule` backup. Manual `workflow_dispatch` is the only fallback. |
| 2 | Is this a manual `workflow_dispatch` with `dry_run` still checked? | `daily_ipo_alert.yml` / `check_allotment.yml` step expression | Print-only; no send, no audit write | Live send (unchecked box, or any non-`workflow_dispatch` event) |
| 3 | Did discovery return 0 rows on **both** boards? | `run_scan()` / `scrape_all_open_ipos()` | `send_failure_alert()` (HTML likely changed) | Continue |
| 4 | Does today have any IPO with `close_date == today IST`? | `_select_candidates()` / `closing_on()` | Score them | Silent no-op (normal) |
| 5 | Did this IPO's detail+GMP scrape succeed? | `_score_one()` / `scrape_one()` | Continue to live sub + score | `SCAN ERROR` card (not a SKIP) |
| 6 | Did live subscription fetch succeed? | `fetch_live_subscription()` | Overwrite `sub_*_x` on the master row | Warning appended to `parse_warnings`; score anyway |
| 7 | Did `score_features()` succeed? | `_score_one()` | Fill `p_pop` / `apply_s1` / EV / quality | `SCAN ERROR` card |
| 8 | Are the `{board}_s1.pkl` bundles actually on disk? | `Scorer._bundle()` | Real `p_pop` / `apply_s1` | `apply_s1=False`, `p_pop=None` — looks like SKIP |
| 9 | Is live GMP scrapeable right now? | `gmp_rs` empty / NaN → `format_card()` / `_fmt_gmp_amount()` | Show ₹ amount + as-of | Card says `GMP: not available` (never 0) |
| 10 | Has this `(ipo_id, close_date)` already been alerted today? | `records_needing_alert()` via canonical `_audit_key` | Skip send for that IPO; later `upsert_audit()` may still refresh the row | Candidate for dispatch |
| 11 | `--dry-run`? | `run_scan()` / `dispatch()` | Print cards; skip send **and** skip audit write | Real send path |
| 12 | Did Telegram send succeed? | `dispatch()` → `send_telegram()` | Count toward `sent` | Unconfigured → `False` (no-op). Exception → logged, continue to email |
| 13 | Did email send succeed? | `dispatch()` → `send_email()` | Digest delivered | Unconfigured → `False` (no-op). Exception → `email_error` |
| 14 | Did **both** real channels throw (`sent == 0` and `email_error` set)? | `dispatch()` | Raise `NotificationDeliveryError` → workflow red; **audit row not written** | At least one channel delivered, or both were merely unconfigured |
| 15 | Scanner exception after all that? | `main()` | `send_failure_alert()` then re-raise (Actions red). Commit step still runs (`if: always()`) so `live_alert_state.json` can be pushed | Exit 0 |
| 16 | Is this row already `verified`? | `run_verify()` | Leave it | Re-fetch detail URL |
| 17 | Has listing OHLC (open price) published yet? | `compute_actuals()` | Write actuals, flip `verified` | Return row unchanged; next weekday retries |
| 18 | Open-return ≥ 15% **and** low held above issue (or low missing)? | `compute_actuals()` → `actual_is_clean_pop` | `True` | `False`. Not the historical `listing_day_gain_pct` target — see J.4 |
| 19 | Is this audit row inside 1–4 IST days after close, and not already `allotment_notified`? | `is_allotment_due()` | Inspect the detail page | Skip (too new, too old, or already pinged) |
| 20 | Is allotment out? (BoA heading/link, else timetable date) | `allotment_is_out()` / `parse_allotment_published()` | Dispatch path | Leave row; retry tomorrow while still in-window |
| 21 | Is `PAN_PROFILES` set and parseable? | `load_pan_profiles()` | Per-person registrar lookup | Generic "allotment out" Telegram + digest email |
| 22 | Does this registrar have an automated checker (KFintech / MUFG Intime)? | `checker_for_registrar()` | Playwright + OCR lookup | No per-PAN result → skip that profile (silent) |
| 23 | Did the captcha OCR resolve (≤ 4 attempts)? | `_with_retries()` / `solve_captcha()` | Parse the result blob | `captcha_failed` — silent for that PAN |
| 24 | Was this a page-structure / selector break rather than an OCR miss? | `_with_retries()` except-path | Status `lookup_failed`, **no** captcha-style retry | (captcha path above) |
| 25 | Is the result confirmable `allotted` or `not_allotted`? | `EMAIL_STATUSES` | Personalized email to **that profile's** address (no PAN in the body) | `no_application` / `captcha_failed` / `lookup_failed` / `company_not_found` → silent skip |
| 26 | Did **every** lookup in a batch of ≥2 fail, with zero resolved answers? | `raise_if_systematic_lookup_failure()` | Raise `RegistrarLookupBatchError` → workflow red; `allotment_notified` **not** set | One-off misses stay silent; `no_application` counts as resolved |
| 27 | Did every personalized email that was actually attempted throw? | `dispatch_pan_results()` | Raise `NotificationDeliveryError`; flag not persisted | At least one email sent, or nobody reached `EMAIL_STATUSES` (intentional skip, flag **is** set so we don't retry forever) |
| 28 | Was anyone in this PAN batch emailed? | `dispatch_pan_results()` | Count-only Telegram summary. Telegram failure here is printed, not raised | No Telegram (silent) |

The through-line is the same honesty rule as Parts F–H: the live system is a statistical
alerter with a paper-trade log, not a guarantee. Part H's first improvement point ("start
a live paper-trading log now") is exactly `data/live_audit_log.csv` +
`data/analysis/live_performance.json`. Whether live-GMP-driven scores stay calibrated
against those realized open-price pops is still an open measurement — the backtest cannot
answer it, and the live scorecard is how you eventually will.
