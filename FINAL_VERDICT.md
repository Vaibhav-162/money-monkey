# Final Verdict: Did the IPO Scoring Algorithm Work?

*Written after the full pipeline ran end-to-end: master sheet (1,944 IPOs, 2016-2026) →
leak-free GMP re-scrape → post-listing price fetch → walk-forward trained models.*

## TL;DR

**Strategy 1 (listing-day pop) is a real, usable statistical edge — with two honest caveats.**
It clearly beats "apply to everything" and mostly beats the simple friend-rules, but its best
feature (grey market premium) is still measured with a leaky proxy for 95% of the data, and
year-to-year results swing a lot because IPO counts per year are small.

**Strategy 2 (long-term hold) did not work.** After finally getting real 6-month stock-price
data, the model's top picks did not clearly beat just buying a random basket of IPOs. Some of
its best-looking numbers (SME) are almost certainly a mirage caused by which SME stocks even
have price data available, not genuine skill.

**Bottom line for real money:** use Strategy 1 as a *ranking/filter tool with a human sanity
check*, not an auto-pilot. Do not act on Strategy 2's numbers as-is. And a ranking that never
arrives is worth zero — the live delivery pipeline is a separate question, previously broken,
now honest-when-it-fails but no longer redundant. See §6.

---

## 1. What we actually built

- Scraped 1,944 historical IPOs (Chittorgarh), 1,909 kept after dropping 35 FPOs/REITs/InvITs
  (correctly excluded — those aren't IPOs and don't belong in this dataset).
- Two separate models per strategy, one for Mainboard and one for SME (4 models total), each
  trained only on its own board — never mixed.
- Strategy 1 target: did the IPO "pop cleanly" (gain ≥15% at listing, and the price never
  dipped below issue price that day)?
- Strategy 2 target: did the stock beat the Nifty 50 by more than 5% over the next ~6 months
  (126 trading sessions), using real fetched stock prices, not the messy scrape-day snapshot?
- Validation: walk-forward by year only (train on past years, test on the next year), never
  random shuffling — this is the only honest way to test a model that will be used on the
  *next* IPO, not a random past one.
- A small automatic hyperparameter search per board (not a hand-picked guess), SHAP feature
  importance, and a real regression model for "how much will it pop" (not just yes/no).

This part of the plan was executed properly. The architecture is sound. The problems below are
about the **data**, not sloppy engineering.

---

## 2. What worked

### Strategy 1 clearly beats doing nothing

| Rule | Mainboard n | Mainboard pop rate | SME n | SME pop rate |
| --- | --- | --- | --- | --- |
| Apply to every 2020+ IPO | 421 | 44.9% | 992 | 44.4% |
| Live-feasible rule (GMP + subscription + size — no paywalled data) | 59 | 91.5% | 253 | 91.7% |
| The full trained model, top decile (avg across years) | — | ~59-88%¹ | — | ~87% |

¹ Mainboard precision swings a lot year to year (see §3) — SME is the more stable board.

The model roughly **doubles your win rate** versus applying blindly, and it does this using
only information a bot could actually read at 3:30pm on close day (no paywalled QIB/Retail
breakdown, no intraday GMP trend — both were correctly excluded rather than faked).

The single strongest driver in both boards, confirmed by SHAP, is **`gmp_pct_vs_issue`**
(grey market premium as % of issue price) followed by **`sub_total_x`** (total subscription).
This matches basic market intuition and is a good sanity check that the model learned something
real, not noise.

### The "friend rules" partially hold up, especially for SME

Rule 1 (GMP ≥50%, subscription ≥50x, issue size <₹600 Cr) found only 25 Mainboard IPOs in six
years but hit a **100% pop rate**. On SME it found 152 IPOs at a **99.3% pop rate**. That's a
real signal — but for Mainboard, 25 examples in 6 years is too few to trust blindly; you'd wait
months between applying to anything if you only used this rule.

### The Expected-Value framework matters, and the numbers show why

A model that only chases "biggest possible gain" is a trap: those IPOs get 50-100x oversubscribed,
so your odds of actually *getting shares* collapse. The Expected-Value scorer accounts for
this — that's why `p_allot` (allotment probability) shows up as a top-5 SHAP feature everywhere.
This is the single most important practical fix versus the original "friend rules," which never
accounted for allotment odds at all.

---

## 3. What went wrong / the real bottlenecks

### Bottleneck #1: The grey-market data we most need barely exists as history

This was the single biggest technical fight in this project, and the result is disappointing:

- Out of 1,909 IPOs, only **47** ended up with a genuinely leak-free "GMP as of close day"
  value (`gmp_anchor_ipo_close`). **997** are still falling back to a listing-day GMP number,
  which is *closer* to the truth than nothing, but is technically measured one or more days
  *after* you'd need to decide.
- We verified directly on InvestorGain's own site: their "GMP Performance Tracker" report shows
  **zero records for 2021, 2022, and 2023** — it isn't a scraping bug, they simply don't keep a
  browsable historical GMP archive that far back for most IPOs, especially small SME names.
  Even a large, well-known 2022 mainboard IPO (Global Health/Medanta) came back with no archive.
- Practically: the model's #1 feature is measured cleanly for only about **2.5%** of the
  dataset. For the rest, we're using a reasonable-but-imperfect stand-in.

This isn't a bug we can code our way out of — it's a hole in the source data itself. The honest
fix is: going forward, *every future IPO you score live* will have a clean, real-time GMP number
(you'll be scoring it in real time, not retroactively), so this weakness matters much more for
*validating the model's history* than for *using it live*.

### Bottleneck #2: Small sample sizes make Strategy 1's Mainboard numbers noisy

Mainboard walk-forward precision@top-10% by year: 25% → 33% → 44% → 100% → 100%. That is not a
stable trend — it's what happens when each test year only has 38-104 Mainboard IPOs and the
"top 10%" is sometimes only 4-9 companies. One or two surprise outcomes swing the number by
25+ percentage points. SME is much more stable (year-to-year swing of roughly ±13 points instead
of ±33) simply because there are 2-3x more SME IPOs per year to test against.

**Practical meaning:** trust the SME Strategy-1 numbers more than the Mainboard ones. Mainboard
IPOs are rarer, so the model has less to learn from and less to be tested against.

### Bottleneck #3: Strategy 2 (long-term hold) simply did not produce a usable edge

This is the honest disappointing result. After actually fetching 6-month post-listing prices
(not the scrape-day snapshot the original "friend rules" implicitly relied on):

| Board | Model's top-30% picks: 6-month excess return | Just holding *everything*: 6-month excess return | Hit ratio (beat Nifty +5%) |
| --- | --- | --- | --- |
| Mainboard | **+4.2%** | +7.9% | 43.5% |
| SME | +21.3% (± 21.9% — as much noise as signal) | +22.8% | 39.4% |

On **Mainboard, the model's own top picks did worse than a random basket of all IPOs.** That is
about as clear a "this isn't working" signal as a backtest can give. A hit ratio of 39-43%
(below 50%) means the model is *worse than a coin flip* at picking 6-month winners.

The SME `cagr_proxy` number (127% average, ± 100%!) looks spectacular in the summary file, but:

1. The error bar is almost as big as the number itself — a handful of huge-return outlier IPOs
   are dragging the whole average.
2. **The most recent SME IPOs (2025, 2026) had too few matured price histories to even test**
   (2 and 0 usable rows) — meaning the model was never validated on the exact period you'd
   actually want to use it for.
3. Our own price spot-check found **0 of 10** recent SME IPOs had any Yahoo-fetchable price
   data at all. The 992 SME rows that *do* have price data are whichever SME stocks happen to
   be trackable on free data sources — probably the larger, more liquid, longer-listed names.
   That's a survivorship bias: the "winners we can even measure" are not a random sample of all
   SME IPOs.

**Conclusion: Strategy 2's numbers are not trustworthy enough to act on**, particularly for
SME, and Mainboard actively underperformed a naive "just hold everything" approach.

### Bottleneck #4: Free/scriptable data sources have real, permanent gaps for SME

- Yahoo Finance essentially does not carry BSE SME quotes (`.BO` suffix) at all.
- `jugaad-data` (NSE-direct) was tried as a fallback and also came back empty for every one of
  10 spot-checked recent SME names.
- This is not fixable with more engineering effort within a free-data-source constraint. A paid
  data vendor (e.g. an NSE/BSE data subscription) would be required to close this gap.

---

## 4. Is this the algorithm's fault, or the data's fault?

Mostly the data. To be specific:

- **The modeling approach (LightGBM + logistic baseline, walk-forward validation, per-board
  split, EV framework, allotment-probability adjustment) is sound and matches how a
  professional would approach this problem** with this amount of data. Nothing here is
  "hacky" or a shortcut.
- **Strategy 1's remaining weakness is a measurement problem** (leaky GMP for 95%+ of history),
  not a modeling problem. It will get *better*, not worse, once used live, because live GMP is
  measured in real time.
- **Strategy 2's failure is a fundamental data-availability problem.** You cannot backtest or
  train a 6-month-hold strategy without 6-month stock prices, and for the SME segment those
  prices mostly don't exist in free sources. No amount of tuning fixes a target variable you
  can't reliably measure.

---

## 5. Can this be used in real life?

### Strategy 1 (listing-day pop): Yes, with guardrails

- Use `score_features()`'s `apply_s1` and `ev_retail` as a **ranking and filtering tool**, not
  an auto-apply signal.
- Trust it more for SME than Mainboard (more stable historically).
- Because live GMP will be measured cleanly (unlike 95% of the training history), real-world
  precision could genuinely be a bit *better* than the backtest shows — but there's no way to
  prove that until you track live predictions for a few months.
- Never forget the core lesson already baked into the EV framework: a huge pop with 80x
  subscription still might not be worth applying for, because you probably won't get shares.

### Strategy 2 (long-term hold): No, not as-is

- Do not use `apply_s2` or `s2_score` to decide whether to hold an IPO for 6+ months. The
  backtest shows it isn't better than doing nothing (Mainboard) or is built on an unreliably
  small, biased sample (SME).
- If you want a real Strategy 2 eventually, the blocker is **data, not code**: you would need a
  paid price-data feed with real SME coverage, and you'd need to wait for 2025-2026 IPOs to
  actually mature 6 months before you can test on the period that matters most.
- Until then, treat the existing `quality_score` (ROE, debt/equity, subscription, OFS ratio) as
  a basic fundamentals checklist — useful as a sanity filter, not as a return forecast.

### Overall

This is a legitimate, honestly-validated **short-term (listing-day) tool** sitting on top of a
**long-term (6-month-hold) tool that isn't ready**. That's a real, if partial, result — and
knowing *which half* to trust is exactly the value of having done this analysis properly instead
of trusting the "friend rules" at face value.

That ranking still has to *reach you* on close day, before typical 4:00–4:30 PM IST broker
cutoffs. That is a pipeline question, not a model question. See §6.

---

## 6. Is the live delivery pipeline itself trustworthy?

Sections 1–5 answered a statistical question: is Strategy 1 / Strategy 2 a good predictor.
This section answers a different one: **does a good prediction actually reach a human in time
to act on it.** Both must be true for this system to be useful for real money. A great model
with a broken alert pipeline is worthless. A perfectly reliable pipeline delivering a bad
model's picks is also worthless. Strategy 2 already fails the first test (§3, §5). The rest of
this section is about whether Strategy 1 can clear the second.

The walk-forward backtest never sent a Telegram. It never raced a 4:00 PM IST UPI cutoff. It
never depended on GitHub Actions, a PAT, or cron-job.org. **Passing the backtest does not imply
the production bot works.**

### What was found broken, and what was fixed

The first production deployment had a silent total failure: GitHub Actions runs went **green**
(no error shown) while delivering neither Telegram nor email. Notification-send exceptions were
caught and swallowed. A dead Gmail app password or a bad Telegram token looked exactly like a
healthy weekday with nothing to report.

Two code fixes closed that class of bug:

1. **`NotificationDeliveryError`** (in `scripts/notify.py`, wired into `scripts/live_scanner.py`
   and `scripts/check_allotment.py`) now fails the workflow **loudly** when a real (non-dry)
   batch loses every attempted channel — zero Telegram delivered, and email also throwing. A
   red Actions run is the alarm. Unconfigured secrets still return a silent no-op
   (`send_telegram` / `send_email` return `False` without raising); that is intentional for a
   Telegram-only or email-only setup, and it is also a remaining trap if *both* channels were
   never configured in the first place.
2. **`upsert_audit()` now runs after the send.** The presence-only gate treats any existing
   `(ipo_id, close_date)` row as "already alerted." Writing the row *before* a failed send used
   to consume the day's one slot, so a credentials fix could not retry. Same ordering on the
   allotment checker: `allotment_notified` is set only after dispatch returns. A failed send
   now leaves the slot open.

Partial delivery (Telegram worked, email threw) still exits 0. That is by design — one channel
got through — but it means a dead Gmail login will not paint the daily-alert job red if
Telegram is healthy.

### The cron finding, the fix, and the tradeoff

GitHub Actions' internal `schedule` trigger, previously used by all three workflows, was
independently found to be unreliable on this public repo: ticks ran hours later than
scheduled, or were dropped entirely, sometimes for days. For a 3:30 PM IST close-day card that
has to land before typical 4:00–4:30 PM IST broker UPI cutoffs (and is worthless after the
5:00 PM exchange close), a 7 PM "backup" is not a backup.

The fix: **all three workflows are now triggered only by an external cron-job.org
`repository_dispatch` POST** (fine-grained PAT, Contents read/write on this repo only, never
committed). `workflow_dispatch` remains as the manual-only fallback. There is **no automatic
backup** if that one POST is missed — expired PAT, cron-job.org outage, misconfigured job, or
a typo in the event type.

| Job | What a miss costs | Old automatic triggers | New automatic triggers |
| --- | --- | --- | --- |
| Daily close-day alert (`live_scanner.py`) | That IPO's bid window is gone | 3 GitHub `schedule` ticks (3:15 / 3:30 / 4:00 IST) plus 1 external POST | Exactly 1 external POST |
| Allotment check (`check_allotment.py`) | One weekday of delay; the 1–4 day post-close window can still catch it tomorrow | 1 GitHub `schedule` (12:00 IST) | Exactly 1 external POST |
| Listing-outcome verify (`verify_outcomes.py`) | Forward-test ledger lags a day; no money decision | 1 GitHub `schedule` (09:45 IST) | Exactly 1 external POST |

This trades **"internally redundant but individually unreliable"** (three GitHub ticks for the
daily alert alone, any one of which might fire hours late or not at all, but with three
chances) for **"individually more punctual but a single point of failure"** (exactly one
automatic trigger, zero automatic retry).

**Is the trade worth it?** For the close-day job, yes — with eyes open. Punctuality *is* the
product. Three clocks that independently arrive after the cutoff were never three chances at a
usable alert; they were three chances at a post-mortem. One clock that actually fires at
3:30 PM IST is the correct primary.

It is a worse trade than it needed to be. Allotment and verify are not racing a 60-minute
window; dropping their GitHub `schedule` was consistency, not necessity. And even on the
close-day job, **a rare late alert still beats a silent total miss** — you at least know an
IPO closed today, instead of reading silence as "nothing to do." The honest de-risking is not
"trust cron-job.org." It is:

- Turn on cron-job.org's own failure-notification email so a missed POST is visible without
  opening GitHub.
- Watch the Actions tab / your inbox during the 3:30–4:00 PM IST window on weekdays. No run by
  ~3:35 PM IST means click **Run workflow** on `main` with `dry_run` **unchecked**. A next-day
  retry cannot bid.
- Optionally restore **one** deliberately low-frequency GitHub `schedule` tick per workflow as
  a last-resort safety net, even knowing it is individually unreliable. The presence-only gate
  already prevents a double-send if the external POST already succeeded. That is the design
  this repo almost had, before the GitHub ticks were removed entirely. **I would put one back
  on the close-day job.** The current "external only" choice over-corrected.

### Fewer triggers does not mean weaker gating

`records_needing_alert()` allows **exactly one alert per `(ipo_id, close_date)`**, no matter
how many attempts happen that day. It is a presence check, not a field-level diff: the first
real write of the day is the one send; later ticks are silent even if GMP/Sub moved.

Going from "up to 4 automatic trigger attempts/day" down to "exactly 1" does **not** change
that correctness. It only changes how much redundancy protects against a single miss. Do not
conflate "fewer triggers" with "less safe gating logic." The gate is the same. The spare
attempts are gone.

Manual `workflow_dispatch` defaults to `--dry-run` so an off-hours test cannot burn that one
slot. Recovering a real miss requires unchecking `dry_run`. A dispatch from cron-job.org is
always live.

### A red allotment run is an alarm, not a bug

`raise_if_systematic_lookup_failure` (in `chittorgarh/registrar_allotment.py`, wired into
`scripts/check_allotment.py`) escalates when **every** PAN lookup in a batch failed (2+
attempts, zero resolved answers — allotted / not-allotted / no-application). A single captcha
miss is normal OCR noise. The same failure on every profile is a strong signal that the
registrar page, the captcha widget, or Tesseract itself broke — not that every applicant
happened to fat-finger the captcha.

A red workflow run here is **deliberately the alarm**. Do not "fix" it by catching the
exception so the job goes green. Unsupported registrars and an empty `PAN_PROFILES` list do
not escalate (nothing was attempted).

### Is this safe to rely on for real money, today?

**No, not as a fire-and-forget bot. Yes, as a ranking tool whose delivery you personally watch
during the close-day window.**

The silent-green-run class of bugs is fixed. The remaining failure mode is a **missed
trigger**, which is visible in the Actions tab if someone looks, and invisible if nobody does.
That is a better class of failure than fake success — but only with a human in the loop.

Pipeline checklist (this is *in addition to* the Strategy 1/2 statistical guardrails in §5):

| Check | If this is not true |
| --- | --- |
| All **three** cron-job.org jobs are configured and actually firing — daily alert, allotment check, *and* outcome verify — not just the 3:30 PM one | Allotment pings and the live paper-trade ledger die silently while close-day cards still look healthy |
| cron-job.org's own failure-alert email is on | The new single clock can fail with nobody notified except GitHub, which you are not watching |
| The PAT is unexpired, fine-grained, Contents read/write on **this repo only** | Every automatic run stops on the same day. There is no GitHub `schedule` left to catch it |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` **and** Gmail (`GMAIL_USER` + app password, plus `ALERT_EMAIL_TO` if the digest should reach more than the sender) are both set | One channel's outage equals total silence. Unset secrets are still a green no-op |
| Someone looks at Actions / Telegram / email around 3:30–4:00 PM IST on weekdays | A genuine double-miss (external POST failed, and you didn't notice) is a lost bid with no retry |
| Recovering a miss uses **Run workflow** on `main` with `dry_run` unchecked | Default manual runs print cards and send nothing, while looking like you "ran it" |
| `data/analysis/models/*.pkl` are committed | The scanner still sends cards, but Strategy 1 silently degrades to `SKIP` / no `p_pop` |
| Actions has **Read and write** contents permission | Audit CSV cannot be committed; retries and allotment flags drift |

Residual risk that remains after every fix above:

- The clock is now one POST. Expired PAT, cron-job.org outage, or a misconfigured job is a
  total automatic miss. That was a deliberate trade. It is not theoretical.
- HTML changes on Chittorgarh or InvestorGain still break discovery / GMP. Zero rows on
  *both* boards fires `send_failure_alert()`; zero closers *today* is a silent no-op
  (correct). A site break that still returns rows with empty fields is a fuzzier failure.
- Registrar captcha OCR is best-effort. Individual misses stay silent; only a full-batch wipe
  goes red.
- The pipeline will faithfully deliver Strategy 2 hold/flip copy. That copy should still be
  ignored (§5).
- Cards are mid-afternoon snapshots. GMP and subscription can still move in the last hour.
  The bot is not the 6 PM print.

**Bottom line for the pipeline:** do not confuse "the model has an edge" with "the bot will
text me in time." The delivery path is now honest when it fails to send, and punctual when the
external clock fires. It is not redundant. Treat a quiet 3:35 PM IST as a possible outage, not
as "no IPO today."

---

## 7. If you want to keep improving this

1. **Track live predictions going forward.** Every time you score a real IPO, save the
   prediction and check the actual outcome later. This builds a leak-free, real-time-GMP
   dataset that will eventually be far more trustworthy than the historical backtest.
2. **Don't re-run the GMP/price scrapers hoping for a different answer** — we've confirmed
   the ceiling on free-source coverage (InvestorGain's own archive is empty for 2021-2023;
   Yahoo/jugaad have near-zero SME coverage). More scraping attempts will not change this.
3. **If Strategy 2 matters to you, budget for a paid NSE/BSE data feed** — that is the actual
   blocker, not the modeling code.
4. **Re-run `run_analysis.py` yearly** as more IPOs mature, so the walk-forward folds keep
   growing and the year-to-year noise (especially on Mainboard) settles down.
5. **De-risk the live clock.** Turn on cron-job.org's failure emails for all three jobs.
   Consider putting back one low-frequency GitHub `schedule` tick on the close-day workflow as
   a last-resort safety net — a late alert beats a silent miss. See §6.
