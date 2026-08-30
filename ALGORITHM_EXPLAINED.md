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
