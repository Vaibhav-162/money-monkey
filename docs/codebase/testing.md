# Testing

## Offline (no network)

```powershell
python -m pytest tests/test_normalize.py tests/test_parse_lohia.py tests/test_master_sheet.py -q
```

`pytest.ini` skips tests marked `smoke` by default.

Parser tests use `tests/fixtures/lohia_2574.html`. That file is test data, not scrape output.

## Live smoke

```powershell
python scrape_ipos.py --smoke
```

Checks Lohia Corp (`ipo_id=2574`) through tracker, detail parse, GMP tab, and master-sheet headers.

Writes to a **temp folder** and deletes it. It must not leave `data/smoke/` or extra CSVs in the repo.

Pytest live test (optional):

```powershell
python -m pytest tests/test_smoke_e2e.py -m smoke
```

That uses pytest’s `tmp_path`, not `data/smoke/`.

## Side effects to avoid

- Do not write satellite CSVs.
- Do not keep Playwright open after a function returns (`browser.chromium_page`).
- Do not start the 2016–2026 scrape unless the user asks.
- A `--smoke` run should not create `data/`.
- Always append/update `ipos.csv`. Rebuild `ipos.xlsx` from CSV (`--rebuild-xlsx` or end of scrape). Never wipe the master CSV on a new scrape.
