import json

from chittorgarh.registrar_allotment import (
    _with_retries,
    checker_for_registrar,
    find_company_option,
    load_pan_profiles,
    mask_pan,
    parse_result_blob,
)


def test_mask_pan_hides_middle() -> None:
    assert mask_pan("ABCDE1234F") == "ABCDE***4F"
    assert "1234" not in mask_pan("ABCDE1234F")
    assert "123" not in mask_pan("ABCDE1234F")
    assert mask_pan("abcde1234f") == "ABCDE***4F"
    assert mask_pan("") == "****"
    assert mask_pan("AB") == "****"


def test_find_company_option_fuzzy_and_none() -> None:
    options = ["--Select--", "Kwick Forensic Solutions Limited", "Lumino Industries Ltd"]
    assert find_company_option(options, "Kwick Forensic Solutions") == "Kwick Forensic Solutions Limited"
    assert find_company_option(options, "Lumino Industries IPO") == "Lumino Industries Ltd"
    assert find_company_option(options, "Totally Unrelated Cement") is None
    assert find_company_option([], "Kwick") is None


def test_load_pan_profiles_skips_malformed(monkeypatch, capsys) -> None:
    payload = [
        {"label": "Me", "pan": "ABCDE1234F", "email": "me@example.com"},
        {"label": "Bad", "pan": "not-a-pan", "email": "bad@example.com"},
        {"label": "NoMail", "pan": "PQRST5678G", "email": ""},
        "skip-me",
    ]
    monkeypatch.delenv("PAN_PROFILES", raising=False)
    out = load_pan_profiles(json.dumps(payload))
    assert out == [{"label": "Me", "pan": "ABCDE1234F", "email": "me@example.com"}]
    logged = capsys.readouterr().out
    assert "skipped malformed PAN" in logged
    assert "ABCDE1234F" not in logged
    assert "PQRST5678G" not in logged
    assert "not-a-pan" not in logged
    assert load_pan_profiles("not-json") == []
    assert load_pan_profiles("") == []
    monkeypatch.setenv("PAN_PROFILES", json.dumps(payload))
    assert load_pan_profiles()[0]["email"] == "me@example.com"


def test_parse_result_blob_statuses() -> None:
    assert parse_result_blob("Invalid Captcha. Try again")["status"] == "captcha_failed"
    assert parse_result_blob("No Application Found")["status"] == "no_application"
    assert parse_result_blob("Status: Not Allotted") == {"status": "not_allotted", "shares": 0}
    allotted = parse_result_blob("Congratulations. Shares Allotted 1600")
    assert allotted["status"] == "allotted"
    assert allotted["shares"] == 1600
    assert parse_result_blob("")["status"] == "captcha_failed"
    assert parse_result_blob("Unexpected registrar HTML")["status"] == "lookup_failed"


def test_with_retries_only_repeats_captcha_failed() -> None:
    n = {"i": 0}

    def eventually_ok():
        n["i"] += 1
        if n["i"] < 3:
            return {"status": "captcha_failed", "shares": None}
        return {"status": "allotted", "shares": 10}

    assert _with_retries(eventually_ok) == {"status": "allotted", "shares": 10}
    assert n["i"] == 3

    n2 = {"i": 0}

    def terminal():
        n2["i"] += 1
        return {"status": "not_allotted", "shares": 0}

    assert _with_retries(terminal)["status"] == "not_allotted"
    assert n2["i"] == 1

    n3 = {"i": 0}

    def always_fail():
        n3["i"] += 1
        return {"status": "captcha_failed", "shares": None}

    assert _with_retries(always_fail)["status"] == "captcha_failed"
    assert n3["i"] == 4

    n4 = {"i": 0}

    def missing_company():
        n4["i"] += 1
        return {"status": "company_not_found", "shares": None}

    assert _with_retries(missing_company)["status"] == "company_not_found"
    assert n4["i"] == 1

    n5 = {"i": 0}

    def unrecognized():
        n5["i"] += 1
        return {"status": "lookup_failed", "shares": None}

    assert _with_retries(unrecognized)["status"] == "lookup_failed"
    assert n5["i"] == 1


def test_checker_for_registrar_supported_and_unsupported() -> None:
    assert checker_for_registrar("KFin Technologies Limited") is not None
    assert checker_for_registrar("MUFG Intime India Pvt.Ltd.") is not None
    assert checker_for_registrar("Link Intime") is not None
    assert checker_for_registrar("Bigshare Services Pvt.Ltd.") is None
    assert checker_for_registrar("Cameo Corporate Services") is None
    assert checker_for_registrar("Skyline Financial") is None
    assert checker_for_registrar("Purva Sharegistry") is None
    assert checker_for_registrar("Unknown House") is None
