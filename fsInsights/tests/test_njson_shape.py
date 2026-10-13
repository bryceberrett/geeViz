"""FIADB-API response-shape handling.

Background: ``/fullreport&outputFormat=JSON`` is broken server-side.
EVALIDator raises ``Key Error / Received an Error: 'row'`` while building
its nested row/column structure and returns an HTML error page under
**HTTP 200** — so a naive client sees "200 OK" and tries to parse HTML.
The identical query succeeds as HTML, NHTML, CSV, XML or NJSON, which is
what proves it a serializer bug rather than an outage or a bad request.
Observed against API v2.1.7 (2026-07-30) / FIADB_1.9.4.00.

Two things here are easy to get wrong and expensive to notice:

1. **The format asymmetry.** ``/fullreport`` needs NJSON; the parameter
   endpoints under ``/fullreport/parameters/`` need JSON and *fail* on
   NJSON. Anyone "tidying up" by unifying them breaks one or the other.

2. **GRP ordering.** NJSON flattens to GRP1/GRP2/GRP3, which map
   positionally to pselected/rselected/cselected. Swapping row and column
   transposes every table while still producing plausible-looking
   output — the worst kind of wrong.

These are offline tests over recorded payload shapes; they do not call
the network, so they still pass while the upstream is unwell.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from geeViz.fsInsights.fia import _to_frame  # noqa: E402


# Shape recorded from a live NJSON response:
#   pselected="All live stocking" -> GRP1
#   rselected="Ownership group"   -> GRP2
#   cselected="Forest type group" -> GRP3
NJSON = {
    "citation": "USDA Forest Service, FIA.",
    "metadata": {
        "FIAorRPA": "FIADEF",
        "evalGrps": ["Arizona 042023"],
        "numEstDesc": "0002 Area of forest land, in acres",
        "dbVersion": "FIADB_1.9.4.00",
    },
    "estimates": [
        {"GRP1": "`0001 Overstocked", "GRP2": "`0001 National Forest",
         "GRP3": "`0180 Pinyon / juniper group",
         "ESTIMATE": 658381.29, "SE": 59721.0, "SE_PERCENT": 9.07,
         "PLOT_COUNT": 123, "VARIANCE": 1.0},
        {"GRP1": "`0001 Overstocked", "GRP2": "`0001 National Forest",
         "GRP3": "`0200 Douglas-fir group",
         "ESTIMATE": 5060.27, "SE": 5669.0, "SE_PERCENT": 112.03,
         "PLOT_COUNT": 1, "VARIANCE": 1.0},
    ],
}

LEGACY = {
    "EVALIDatorOutput": {
        "numeratorName": "Area of forest land, in acres",
        "numeratorAttributeNumber": 2,
        "FIAorRPAfilter": "RPADEF as the forest land definition.",
        "selectedInventories": {"stateInventory": ["Arizona 042023"]},
        "row": [{
            "content": "National Forest",
            "column": [{"content": "Pinyon / juniper group",
                        "cellValueNumerator": 658381.29,
                        "cellSE": 9.07,
                        "cellPlotNumerator": 123}],
        }],
    }
}


def _rows(payload, **kw):
    kw.setdefault("max_se_pct", 30.0)
    kw.setdefault("min_plots", 30)
    df = _to_frame(payload, **kw)
    return df.to_dict("records") if hasattr(df, "to_dict") else list(df)


# ── GRP ordering: the transposition hazard ───────────────────────────────

def test_all_three_selections_map_p_r_c_to_grp1_2_3():
    """Pinned against a live response, not inferred from the names."""
    r = _rows(NJSON, selections=(True, True, True))[0]
    assert r["page"] == "`0001 Overstocked"              # pselected -> GRP1
    assert r["row"] == "`0001 National Forest"           # rselected -> GRP2
    assert r["column"] == "`0180 Pinyon / juniper group"  # cselected -> GRP3


# GRP numbering is positional over the selections SENT, so dropping
# pselected shifts everything down one. Recorded live for
# rselected='County code and name', cselected='Forest type group'.
NJSON_RC = {
    "metadata": {"FIAorRPA": "FIADEF", "evalGrps": ["Alabama 012023"],
                 "numEstDesc": "0002 Area of forest land, in acres"},
    "estimates": [
        {"GRP1": "`01001 1001 AL Autauga",
         "GRP2": "`0140 Longleaf / slash pine group",
         "ESTIMATE": 12345.0, "SE_PERCENT": 20.0, "PLOT_COUNT": 40},
    ],
    "subtotals": {
        "GRP1": [{"GRP1": "`01001 1001 AL Autauga", "ESTIMATE": 304659.33,
                  "SE_PERCENT": 13.48, "PLOT_COUNT": 56}],
        "GRP2": [{"GRP2": "`0140 Longleaf / slash pine group",
                  "ESTIMATE": 1184334.0, "SE_PERCENT": 6.07,
                  "PLOT_COUNT": 258}],
    },
    "totals": [{"ESTIMATE": 22963960.0, "SE_PERCENT": 0.51,
                "PLOT_COUNT": 4241}],
}


def test_two_selections_shift_row_to_grp1_and_column_to_grp2():
    """The bug this exists for.

    Assuming fixed slots put the COLUMN value into ``row`` and left
    ``column`` as None whenever pselected was omitted -- the common
    case. The table transposed while still looking plausible.
    """
    rows = _rows(NJSON_RC, selections=(False, True, True))
    detail = [r for r in rows if r["row"] != "Total" and r["column"] != "Total"]
    assert detail, "expected at least one detail cell"
    d = detail[0]
    assert d["row"] == "`01001 1001 AL Autauga"                 # rselected -> GRP1
    assert d["column"] == "`0140 Longleaf / slash pine group"   # cselected -> GRP2
    assert d["page"] is None                                     # no pselected


def test_single_selection_puts_the_row_in_grp1():
    payload = {"estimates": [{"GRP1": "`0140 Longleaf", "ESTIMATE": 1.0,
                              "SE_PERCENT": 5.0, "PLOT_COUNT": 99}]}
    r = _rows(payload, selections=(False, True, False))[0]
    assert r["row"] == "`0140 Longleaf"
    assert r["column"] is None


def test_column_marginals_are_emitted_as_total_rows():
    """Legacy JSON carried "Total" inline; NJSON moves it to subtotals.

    Callers filtering df['row'] == 'Total' (the example notebook does)
    got an empty frame until these were re-emitted.
    """
    rows = _rows(NJSON_RC, selections=(False, True, True))
    totals = [r for r in rows if r["row"] == "Total" and r["column"] != "Total"]
    assert totals, "column marginals must appear as row='Total'"
    lf = [r for r in totals
          if "Longleaf" in str(r["column"])][0]
    # Matches the documented Alabama example in fia.py's docstring.
    assert lf["estimate"] == pytest.approx(1184334.0)
    assert lf["plots"] == 258


def test_grand_total_is_emitted_and_matches_the_documented_example():
    rows = _rows(NJSON_RC, selections=(False, True, True))
    g = [r for r in rows if r["row"] == "Total" and r["column"] == "Total"]
    assert len(g) == 1
    assert g[0]["estimate"] == pytest.approx(22963960.0)
    assert g[0]["plots"] == 4241
    assert g[0]["se_pct"] == pytest.approx(0.51)


# One grouping only. Shape observed live for wc=532023, snum=2,
# rselected='Forest type': NJSON still sends subtotals over GRP1, and with
# no column to total across they are the estimates again, row for row.
NJSON_R = {
    "metadata": {"FIAorRPA": "FIADEF", "evalGrps": ["Washington 532023"],
                 "numEstDesc": "0002 Area of forest land, in acres"},
    "estimates": [
        {"GRP1": "`0201 Douglas-fir", "ESTIMATE": 8699497.93,
         "SE_PERCENT": 1.77, "PLOT_COUNT": 2651},
        {"GRP1": "`0321 Western larch", "ESTIMATE": 398508.35,
         "SE_PERCENT": 9.45, "PLOT_COUNT": 183},
    ],
    "subtotals": {
        "GRP1": [
            {"GRP1": "`0201 Douglas-fir", "ESTIMATE": 8699497.93,
             "SE_PERCENT": 1.77, "PLOT_COUNT": 2651},
            {"GRP1": "`0321 Western larch", "ESTIMATE": 398508.35,
             "SE_PERCENT": 9.45, "PLOT_COUNT": 183},
        ],
    },
    "totals": [{"ESTIMATE": 22023464.22, "SE_PERCENT": 0.52,
                "PLOT_COUNT": 5905}],
}


def test_one_way_table_lists_each_row_once():
    """A row marginal is only a marginal when there is a column to total.

    Without one, re-emitting the subtotals as column='Total' listed every
    forest type twice, doubled reliable_count/unreliable_count in
    fia_estimate, and would double any sum the model took over the rows.
    """
    rows = _rows(NJSON_R, selections=(False, True, False))
    larch = [r for r in rows if "Western larch" in str(r["row"])]
    assert len(larch) == 1
    assert larch[0]["estimate"] == pytest.approx(398508.35)
    assert len(rows) == 3  # two forest types and the grand total
    g = [r for r in rows if r["row"] == "Total"]
    assert len(g) == 1 and g[0]["estimate"] == pytest.approx(22023464.22)


def test_se_pct_uses_se_percent_not_absolute_se():
    """SE and SE_PERCENT are both present and wildly different.

    The reliability floors compare against a PERCENTAGE, so picking the
    absolute SE would make almost everything look catastrophic (59721 vs
    9.07) and suppress good data.
    """
    r = _rows(NJSON)[0]
    assert r["se_pct"] == pytest.approx(9.07)
    assert r["se_pct"] != pytest.approx(59721.0)


# ── Reliability gating still applies to the new shape ────────────────────

def test_single_plot_row_is_flagged_unreliable():
    rows = _rows(NJSON)
    thin = [r for r in rows if r["plots"] == 1][0]
    assert thin["unreliable"] is True
    assert "single plot" in thin["unreliable_reason"]


def test_well_sampled_row_is_not_flagged():
    rows = _rows(NJSON)
    good = [r for r in rows if r["plots"] == 123][0]
    assert good["unreliable"] is False
    assert good["unreliable_reason"] == ""


def test_provenance_comes_from_njson_metadata():
    r = _rows(NJSON)[0]
    assert r["forest_definition"] == "FIADEF"
    assert r["evaluation"] == "Arizona 042023"
    assert "Area of forest land" in str(r["attribute"])


def test_snum_hint_fills_in_when_njson_omits_the_attribute_number():
    """NJSON has no numeratorAttributeNumber; without the caller's snum
    the units lookup gets None and every row loses its units label."""
    r = _rows(NJSON, snum_hint=2)[0]
    assert r["snum"] == 2


# ── Legacy shape must keep working ───────────────────────────────────────

def test_legacy_evalidator_output_still_parses():
    """Kept as a fallback in case upstream repairs the JSON serializer."""
    r = _rows(LEGACY)[0]
    assert r["row"] == "National Forest"
    assert r["column"] == "Pinyon / juniper group"
    assert r["estimate"] == pytest.approx(658381.29)
    assert r["plots"] == 123


def test_unrecognized_shape_raises_a_useful_error():
    with pytest.raises(ValueError) as ei:
        _to_frame({"something": "else"}, max_se_pct=30.0, min_plots=30)
    msg = str(ei.value)
    assert "EVALIDatorOutput" in msg and "estimates" in msg, (
        "the error should name BOTH accepted shapes so the reader knows "
        "which one was expected"
    )


# ── The format asymmetry ─────────────────────────────────────────────────

def test_fullreport_requests_njson_and_vocab_requests_json():
    """Guard against 'unifying' the two on one outputFormat.

    /fullreport is broken on JSON; /fullreport/parameters/* is broken on
    NJSON. They must stay different.
    """
    import re
    from pathlib import Path

    base = Path(__file__).resolve().parents[1]
    fia_src = (base / "fia.py").read_text(encoding="utf-8")
    vocab_src = (base / "vocab.py").read_text(encoding="utf-8")

    m = re.search(r'"outputFormat":\s*"([A-Z]+)"', fia_src)
    assert m and m.group(1) == "NJSON", (
        "fia.estimate must request NJSON — /fullreport&outputFormat=JSON "
        "returns an HTML error page (KeyError 'row') under HTTP 200"
    )
    assert '"outputFormat": "JSON"' in vocab_src, (
        "vocab must request JSON — the parameter endpoints return an "
        "error page for NJSON"
    )
