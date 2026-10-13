"""Fall foliage: a foliage-window composite minus a leaf-on composite.

An AskTerra session (2026-10-08, "how golden are the larches in
Washington right now") loaded COPERNICUS/S2_SR_HARMONIZED by hand with a
QA60/SCL mask, compared stand-wide means rather than a per-pixel change,
and reported larch "peak golden" from a +310% NDYI rise. The instructions'
own Dataset defaults table told it to load that collection directly, in
contradiction of the pitfall further down. And geeViz had no foliage
method, so AskTerra 1.0's terraFoliage (post minus pre, yellowness /
redness change with thresholds) was out of reach.

Rerun in surface reflectance with Cloud Score+, larch yellowness rose
+0.197 and Douglas-fir, which never turns gold, +0.207: the rise was
seasonal, not larch. So the method also tells the agent to compare an
evergreen control.

Live checks of getFoliageChange (Earth Engine) were run by hand; these
tests cover the date logic, the discoverability and the instructions.
"""
import datetime
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTR = (ROOT / "mcp" / "agent-instructions.md").read_text(encoding="utf-8")


def _dates(**kw):
    from geeViz.getImagesLib import _foliageDates
    out = _foliageDates(**kw)
    return [d.strftime("%Y-%m-%d") for d in out]


def test_default_windows_are_the_last_three_weeks_and_six_weeks_earlier():
    today = datetime.datetime(2026, 10, 8)
    assert _dates(today=today) == ["2026-08-06", "2026-08-27", "2026-09-17", "2026-10-08"]


def test_a_given_post_window_shifts_the_pre_window_by_the_gap():
    pre_s, pre_e, post_s, post_e = _dates(startDatePost="2026-10-01", endDatePost="2026-10-15", timeGap=60)
    assert (post_s, post_e) == ("2026-10-01", "2026-10-15")
    assert (pre_s, pre_e) == ("2026-08-02", "2026-08-16")


def test_a_single_given_date_is_completed_from_composite_days():
    pre_s, pre_e, post_s, post_e = _dates(endDatePost="2026-10-08", startDatePre="2026-07-15", compositeDays=14)
    assert (post_s, post_e) == ("2026-09-24", "2026-10-08")
    assert (pre_s, pre_e) == ("2026-07-15", "2026-07-29")


def test_iso_timestamps_are_accepted():
    assert _dates(startDatePost="2026-10-01T00:00:00", endDatePost="2026-10-08T12:00:00",
                  startDatePre="2026-07-01", endDatePre="2026-07-31")[2:] == ["2026-10-01", "2026-10-08"]


def test_a_baseline_that_overlaps_the_foliage_window_is_refused():
    with pytest.raises(ValueError, match="must end before"):
        _dates(startDatePre="2026-09-01", endDatePre="2026-10-01",
               startDatePost="2026-09-20", endDatePost="2026-10-08")


def test_the_change_is_post_minus_pre_and_the_layers_are_thresholded():
    import inspect
    from geeViz import getImagesLib as gil
    src = "\n".join(l for l in inspect.getsource(gil.getFoliageChange).splitlines()
                    if not l.lstrip().startswith("#"))
    body = src[src.index('"""', src.index('"""') + 3) + 3:]   # past the docstring
    assert "post.select(idx).subtract(pre.select(idx))" in body
    assert 'gte(yellowThresh)' in body and 'gte(rednessThresh)' in body
    assert "superSimpleGetS2(studyArea" in body, "S2 must come from superSimpleGetS2, not a hand-built collection"
    assert "multiply(10000)" in body, "Landsat (0-1) must be put on S2's 0-10000 scale before merging"


@pytest.fixture(scope="module")
def srv():
    import geeViz.mcp.server as _srv
    _srv._build_module_tree()
    return _srv


def test_search_codebase_finds_the_foliage_functions(srv):
    fn = getattr(srv.search_codebase, "fn", srv.search_codebase)
    for name in ("getFoliageChange", "foliageIndices"):
        out = json.loads(fn(name=name))
        assert "error" not in out, out
        assert out.get("signature") or out.get("docstring")
    hits = json.dumps(json.loads(fn(query="fall foliage")))
    assert "getFoliageChange" in hits


def _row(label_fragment):
    for line in INSTR.splitlines():
        if line.startswith("|") and label_fragment in line:
            return line
    raise AssertionError(f"no Dataset defaults row for {label_fragment!r}")


def test_the_s2_default_is_supersimplegets2_not_the_raw_collection():
    row = _row('"Sentinel-2", "S2"')
    default_cell = row.split("|")[2]
    assert "superSimpleGetS2" in default_cell
    # Named only to forbid it.
    assert re.search(r"Never load `COPERNICUS/S2_SR_HARMONIZED`", default_cell)


def test_foliage_questions_route_to_the_change_method_with_a_control():
    assert "gil.getFoliageChange" in _row('"fall foliage"')
    i = INSTR.index("### Fall foliage")
    section = INSTR[i:INSTR.index("\n---", i)]
    assert "MINUS" in INSTR[INSTR.index('"fall foliage"'):][:400]
    assert "keeps its color through the season" in section and "report the difference" in section
    assert "Never describe color from foliage-date imagery alone" in section
