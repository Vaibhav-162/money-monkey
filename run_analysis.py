"""Run the local analysis pipeline (no network).

  python run_analysis.py --out data

Needs data/ipos.csv. Uses data/gmp_history.csv and data/prices/returns.csv when present.
Until you run the GMP re-scrape, gmp_at_close falls back to listing-anchored gmp_rs
and is flagged gmp_anchor=listing_date_leaky.

WHAT THIS FILE DOES
--------------------
This is the offline trainer/report CLI — the one command that turns the
master IPO sheet into walk-forward metrics and the pickle files live
scoring loads. It never hits the network. Production live alerts are
`scripts/live_scanner.py`, which does *not* import this file; this file
does *not* import `analysis.score` or `analysis.live_audit`.

Pipeline order: `analysis.load.load_dataset` → `analysis.prices.load_nifty_20d`
→ `analysis.features.add_features` → `analysis.targets.add_targets`, then
quality-checklist eval (`analysis.baselines.evaluate_quality_checklist`),
per-board EDA (`analysis.eda.board_eda`), friend-rule baselines
(`rule1_listing_pop` / `rule2_long_hold` / `live_feasible_rule`),
walk-forward S1/S2 (`analysis.backtest`), a full-history `fit_s1`/`fit_s2`
per board (`analysis.models`) that *overwrites* the last walk-forward
pickle, pooled S1 ablation, hypothetical stop-loss table, and
`analysis.report` dumps under `{--out}/analysis/`. S1's delivered
`apply_threshold` is copied from the last non-skipped walk-forward year.

Re-run this after `scripts/rescrape_gmp_history.py` and
`scripts/fetch_prices.py` so GMP is close-day-anchored and Strategy 2
has 6-month excess returns; until then the summary JSON itself warns
that GMP may be leaky and S2 is on the quality-ranker fallback.

KEY TERMS USED HERE
--------------------
- Leakage: using information that would not have been knowable on close
  day. Listing-anchored GMP (`gmp_anchor=listing_date_leaky`) is the
  recurring case this CLI flags; `gmp_anchor=ipo_close` is the safe one.
- GMP (Grey Market Premium): unofficial pre-listing premium. Close-day
  `gmp_at_close` needs `data/gmp_history.csv`; otherwise `gmp_rs` is
  copied and marked leaky.
- Mainboard vs SME: every EDA, baseline, walk-forward, and delivered
  pickle is per board. The only pooled run is the S1 ablation artifact.
- Walk-forward: yearly train-on-past / test-on-next replay in
  `backtest.py`. Not a random split — IPOs are a time series.
- apply_s1 / apply_s2: the two decisions the scorer exists to produce.
  This CLI trains the pickles and reports historical EV; it does not
  send alerts. Live S2 apply is still the quality checklist, not the
  S2 regressor.
- Quality checklist: 0–4 sanity score evaluated here vs realized
  `exret_126` into `s2_quality_checklist_eval.json`.
- SHAP (`shap_s1` in `summary.json`): last walk-forward fold's top
  features, so a run is inspectable without opening the pickle.
- Pooled ablation / stop-loss sensitivity: comparison tables only —
  not the delivered scorer, not fed back into `fit_*`.
- Baselines: simple friend rules (listing-pop heuristic, long-hold
  heuristic, live-feasible GMP/size/subscription cut) plus "apply
  everything 2020+" so the ML numbers have a dumb comparator.
- Outlier flags: high-sub, SM REIT/unit, price-outside-band, negative
  PAT. Counted in the summary; kept in the model frame, not dropped.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `main(argv)`: argparse (`--out`, default `data`), run the pipeline,
  print `summary.json`, return 0. Creates `{out}/analysis/` including
  `models/{board}_s1.pkl` and `{board}_s2.pkl` for `score_features()`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.baselines import (
    evaluate_quality_checklist,
    live_feasible_rule,
    rule1_listing_pop,
    rule2_long_hold,
    summarize_rule,
)
from analysis.backtest import pooled_ablation_s1, stop_loss_sensitivity, walk_forward_s1, walk_forward_s2
from analysis.eda import board_eda
from analysis.features import add_features
from analysis.load import load_dataset
from analysis.models import fit_s1, fit_s2, predict_s1_proba, save_bundle
from analysis.prices import load_nifty_20d
from analysis.report import dump_json, write_predictions
from analysis.targets import add_targets


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="IPO scoring pipeline (local, no network).")
    p.add_argument("--out", default="data")
    args = p.parse_args(argv)
    out_dir = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    analysis_dir = out_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)

    df, excluded = load_dataset(out_dir)
    df = load_nifty_20d(df, out_dir / "prices")
    df = add_features(df)
    df = add_targets(df)

    checklist_eval = {b: evaluate_quality_checklist(df, b) for b in ("mainboard", "sme")}
    dump_json(analysis_dir / "s2_quality_checklist_eval.json", checklist_eval)

    excluded_path = analysis_dir / "excluded.csv"
    if len(excluded):
        excluded.to_csv(excluded_path, index=False)

    eda = {b: board_eda(df, b) for b in ("mainboard", "sme")}
    dump_json(analysis_dir / "eda.json", eda)

    rules = {}
    for board in ("mainboard", "sme"):
        chunk = df[(df["exchange_type"] == board) & (df["listing_year"] >= 2020)]
        rules[board] = [
            summarize_rule(chunk, rule1_listing_pop(chunk), "friend_rule1_pop"),
            summarize_rule(chunk, rule2_long_hold(chunk), "friend_rule2_hold"),
            summarize_rule(chunk, live_feasible_rule(chunk), "live_total_gmp_size"),
            summarize_rule(chunk, pd.Series(True, index=chunk.index), "all_2020plus"),
        ]
    dump_json(analysis_dir / "baselines.json", rules)

    s1 = {}
    s2 = {}
    for board in ("mainboard", "sme"):
        print(f"walk-forward S1 {board}", flush=True)
        s1[board] = walk_forward_s1(df, board, analysis_dir)
        print(f"walk-forward S2 {board}", flush=True)
        s2[board] = walk_forward_s2(df, board, analysis_dir)
        # final delivered models trained on all 2020+ rows for that board
        train_s1 = df[(df["exchange_type"] == board) & (df["listing_year"] >= 2020)]
        bundle_s1 = fit_s1(train_s1, board)
        if s1[board]["folds"]:
            last = [f for f in s1[board]["folds"] if not f.get("skipped")]
            if last:
                bundle_s1.apply_threshold = last[-1].get("threshold", 0.5)
        save_bundle(bundle_s1, analysis_dir / "models" / f"{board}_s1.pkl")
        train_s2 = df[df["exchange_type"] == board]
        bundle_s2 = fit_s2(train_s2, board)
        save_bundle(bundle_s2, analysis_dir / "models" / f"{board}_s2.pkl")
        proba = predict_s1_proba(bundle_s1, df[df["exchange_type"] == board])
        df.loc[df["exchange_type"] == board, "pred_s1_p_pop"] = proba

    dump_json(analysis_dir / "s1_walkforward.json", s1)
    dump_json(analysis_dir / "s2_walkforward.json", s2)
    print("pooled ablation S1", flush=True)
    ablation = pooled_ablation_s1(df)
    dump_json(analysis_dir / "s1_pooled_ablation.json", ablation)

    stop_loss = {b: stop_loss_sensitivity(df, b) for b in ("mainboard", "sme")}
    dump_json(analysis_dir / "s1_stop_loss_sensitivity_hypothetical.json", stop_loss)

    write_predictions(df, analysis_dir / "predictions.csv")

    leaky = int((df["gmp_anchor"] == "listing_date_leaky").sum()) if "gmp_anchor" in df.columns else 0
    close_ok = int((df["gmp_anchor"] == "ipo_close").sum()) if "gmp_anchor" in df.columns else 0
    outlier_flags = {
        flag: int(df[flag].sum())
        for flag in ("flag_high_sub", "flag_sm_reit_unit", "flag_price_outside_band", "flag_negative_pat")
        if flag in df.columns
    }
    summary = {
        "n_model": int(len(df)),
        "n_excluded": int(len(excluded)),
        "outlier_flags_kept_in_model_not_dropped": outlier_flags,
        "gmp_anchor_ipo_close": close_ok,
        "gmp_anchor_listing_leaky": leaky,
        "has_price_returns": bool("exret_126" in df.columns and df["exret_126"].notna().any()),
        "boards": {
            b: {
                "n": int((df["exchange_type"] == b).sum()),
                "n_2020": int(((df["exchange_type"] == b) & (df["listing_year"] >= 2020)).sum()),
            }
            for b in ("mainboard", "sme")
        },
        "s1_summary": {b: s1[b].get("summary") for b in s1},
        "s2_summary": {b: s2[b].get("summary") for b in s2},
        "s2_mode": {b: s2[b].get("mode") for b in s2},
        "shap_s1": {b: s1[b]["folds"][-1].get("shap_top") if s1[b]["folds"] else None for b in s1},
        "note": (
            "This is a historical statistical scorer, not investment advice. "
            "Until scripts/rescrape_gmp_history.py is run, GMP may be listing-date anchored (leaky). "
            "Until scripts/fetch_prices.py is run, Strategy 2 uses the quality ranker fallback."
        ),
    }
    dump_json(analysis_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2, default=str))
    print(f"wrote {analysis_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
