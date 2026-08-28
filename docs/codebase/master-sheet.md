# Master sheet

One IPO = one row. Excel file: `ipos.xlsx`. CSV twin: `ipos.csv`.

## Header

- Row 1: group titles, merged across their columns in Excel (`Identity`, `Calendar`, `Financials FY1 (latest)`, `Listing day NSE`, …).
- Row 2: short labels (`IPO ID`, `PAT Cr`, `Open`, …).
- Row 3+: data.

CSV uses the same two header rows. Labels repeat (several columns named `Period` or `Open`). `export.read_master` binds columns **by position**, not by label.

Column spec lives in `export.COLUMN_GROUPS` as `(group, field_key, label)`.

## Flattened satellites

These used to be extra CSVs. They are now columns on the IPO row:

| Source | Columns |
| --- | --- |
| Financials (latest 3 periods, newest first) | `fy1_*`, `fy2_*`, `fy3_*` |
| Listing day | `listing_bse_*`, `listing_nse_*` |
| Objects of issue | `object_1` … `object_4` plus amounts |
| OFS sellers | `ofs_1_*` … `ofs_3_*` |
| GMP history length | `gmp_obs_count` plus last close fields |

Do not add `financials.csv` / `kpis.csv` / `listing_day.csv` back unless a later feature needs a second sheet.

## Coverage

`coverage.txt` reports fill rates on the **full master file** (subscription, `fy1_pat`, ISIN, `listing_nse_open`, GMP, industry, ROCE). CFO/FCF is recorded as not published.
