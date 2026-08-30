from datetime import date

from chittorgarh.live_subscription import parse_subscription_html, subscription_url
from chittorgarh.live_dashboard import (
    closing_on,
    dashboard_url,
    parse_dashboard_html,
    parse_issue_dates,
    scrape_open_ipos,
    today_ist,
)

FIXTURE_HTML = """
<html><body>
<table>
  <tr><th>Company</th><th>Issue Date</th></tr>
  <tr>
    <td colspan="2">
      <a href="/ipo/deepa-jewellers-ipo/2827/" title="Deepa Jewellers ">Deepa Jewellers</a>
      <span class="float-end ms-2">01 - 03 Sep</span>
    </td>
  </tr>
  <tr class="color-green">
    <td colspan="2">
      <a href="/ipo/lumino-industries-ipo/2013/" title="Lumino Industries ">Lumino Industries</a>
      <span><span class="badge rounded-pill bg-success" title="Open">O</span></span>
      <span class="float-end ms-2">27 - 31 Aug</span>
    </td>
  </tr>
  <tr class="color-green">
    <td colspan="2">
      <a href="/ipo/priority-jewels-ipo/2435/" title="Priority Jewels ">Priority Jewels</a>
      <span><span class="badge rounded-pill bg-success" title="Open">O</span></span>
      <span class="float-end ms-2">28 Aug - 01 Sep</span>
    </td>
  </tr>
  <tr>
    <td colspan="2">
      <a href="/ipo/purple-style-labs-ipo/2622/" title="Purple Style Labs ">Purple Style Labs</a>
      <span class="float-end ms-2">31 Aug - 02 Sep</span>
    </td>
  </tr>
  <tr class="color-lightyellow">
    <td colspan="2">
      <a href="/ipo/annu-projects-ipo/2500/" title="Annu Projects ">Annu Projects</a>
      <span><span class="badge rounded-pill bg-warning">P</span></span>
      <span class="float-end ms-2">25 - 28 Aug</span>
    </td>
  </tr>
  <tr>
    <td colspan="2">
      <a href="/ipo/shiprocket-ipo/2450/" title="Shiprocket ">Shiprocket</a>
      <span class="float-end ms-2">12 - 14 Aug</span>
    </td>
  </tr>
  <tr>
    <td colspan="2">
      <a href="/ipo/broken-row-ipo/9999/" title="Broken Row">Broken Row</a>
    </td>
  </tr>
</table>
<table>
  <tr><th>Company</th><th>Apply</th></tr>
  <tr><td><a href="/ipo_review/skyways-air-ipo/5197/">Skyways Air IPO</a></td></tr>
</table>
</body></html>
"""

AS_OF = date(2026, 8, 29)


def test_dashboard_urls() -> None:
    assert dashboard_url("mainboard").endswith("/ipo/")
    assert dashboard_url("mainline") == dashboard_url("mainboard")
    assert "a=sme" in dashboard_url("sme")


def test_parse_issue_dates_same_month() -> None:
    open_d, close_d = parse_issue_dates("01 - 03 Sep", AS_OF)
    assert open_d == date(2026, 9, 1)
    assert close_d == date(2026, 9, 3)
    open_d, close_d = parse_issue_dates("27 - 31 Aug", AS_OF)
    assert open_d == date(2026, 8, 27)
    assert close_d == date(2026, 8, 31)


def test_parse_issue_dates_cross_month() -> None:
    open_d, close_d = parse_issue_dates("31 Aug - 02 Sep", AS_OF)
    assert open_d == date(2026, 8, 31)
    assert close_d == date(2026, 9, 2)
    open_d, close_d = parse_issue_dates("28 Aug - 01 Sep", AS_OF)
    assert open_d == date(2026, 8, 28)
    assert close_d == date(2026, 9, 1)


def test_parse_issue_dates_year_wrap_dec_jan() -> None:
    open_d, close_d = parse_issue_dates("28 Dec - 02 Jan", date(2026, 12, 30))
    assert open_d == date(2026, 12, 28)
    assert close_d == date(2027, 1, 2)


def test_parse_issue_dates_january_looks_back_to_december() -> None:
    open_d, close_d = parse_issue_dates("28 - 31 Dec", date(2027, 1, 5))
    assert open_d == date(2026, 12, 28)
    assert close_d == date(2026, 12, 31)


def test_parse_issue_dates_explicit_year() -> None:
    open_d, close_d = parse_issue_dates("01 - 03 Sep 2025", date(2026, 8, 29))
    assert open_d == date(2025, 9, 1)
    assert close_d == date(2025, 9, 3)


def test_parse_issue_dates_garbage_returns_none() -> None:
    assert parse_issue_dates("", AS_OF) == (None, None)
    assert parse_issue_dates("TBA", AS_OF) == (None, None)


def test_parse_dashboard_html_statuses_and_dates() -> None:
    rows = parse_dashboard_html(FIXTURE_HTML, "mainboard", AS_OF)
    by_id = {r["ipo_id"]: r for r in rows}
    assert set(by_id) == {"2827", "2013", "2435", "2622", "2500", "2450", "9999"}
    assert by_id["2013"]["status"] == "open"
    assert by_id["2013"]["close_date"] == "2026-08-31"
    assert by_id["2013"]["company_name"] == "Lumino Industries"
    assert by_id["2013"]["url"].endswith("/ipo/lumino-industries-ipo/2013/")
    assert by_id["2435"]["status"] == "open"
    assert by_id["2435"]["open_date"] == "2026-08-28"
    assert by_id["2435"]["close_date"] == "2026-09-01"
    assert by_id["2827"]["status"] == "upcoming"
    assert by_id["2827"]["close_date"] == "2026-09-03"
    assert by_id["2500"]["status"] == "pending"
    assert by_id["2500"]["close_date"] == "2026-08-28"
    assert by_id["2450"]["status"] == "closed"
    assert by_id["9999"]["open_date"] is None
    assert by_id["9999"]["close_date"] is None
    assert "5197" not in by_id


def test_closing_on_filters_by_as_of() -> None:
    rows = parse_dashboard_html(FIXTURE_HTML, "mainboard", AS_OF)
    assert [r["ipo_id"] for r in closing_on(rows, date(2026, 8, 31))] == ["2013"]
    assert closing_on(rows, date(2026, 8, 29)) == []
    assert [r["ipo_id"] for r in closing_on(rows, date(2026, 9, 1))] == ["2435"]


def test_scrape_open_ipos_accepts_html_without_network() -> None:
    rows = scrape_open_ipos("sme", as_of=AS_OF, html=FIXTURE_HTML)
    assert rows[0]["exchange_type"] == "sme"
    assert any(r["ipo_id"] == "2013" for r in rows)


def test_today_ist_uses_supplied_datetime() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    utc = datetime(2026, 8, 29, 20, 0, tzinfo=ZoneInfo("UTC"))
    assert today_ist(utc) == date(2026, 8, 30)


SUB_HTML = """
<html><body>
<table>
  <tr><th>Investor Category</th><th>Subscription (times)</th></tr>
  <tr><td>Qualified Institutional</td><td>0.06x</td></tr>
  <tr><td>Non Institutional</td><td>8.18x</td></tr>
  <tr><td>Retail Individual</td><td>6.87x</td></tr>
  <tr><td>Total Subscription</td><td>5.14x</td></tr>
</table>
</body></html>
"""


def test_parse_subscription_html_total_and_categories() -> None:
    out = parse_subscription_html(SUB_HTML)
    assert out["sub_total_x"] == 5.14
    assert out["sub_qib_x"] == 0.06
    assert out["sub_nii_x"] == 8.18
    assert out["sub_retail_x"] == 6.87


def test_subscription_url() -> None:
    assert subscription_url("lumino-industries-ipo", "2013").endswith(
        "/ipo_subscription/lumino-industries-ipo/2013/"
    )
