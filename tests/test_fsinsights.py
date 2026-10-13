"""Tests for geeViz.fsInsights.

Deliberately offline. Both upstreams are public government services with
no uptime guarantee — FIADB-API was observed returning HTTP 200 with an
HTML error page across *every* endpoint during development — so a suite
that depends on them is a suite that fails for reasons unrelated to the
code. The bundled vocabularies make that avoidable: discovery,
validation, reliability flagging, and response parsing are all testable
with no network at all.

Network-touching checks belong in a separate, marked suite.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from geeViz import fsInsights as fs  # noqa: E402
from geeViz.fsInsights import fia, lcms, vocab  # noqa: E402


# ── Bundled vocabularies ─────────────────────────────────────────────────

def test_bundled_catalogs_present_and_populated():
    """Discovery must work on a fresh install with no network.

    This is the property that let the package stay useful while the FIA
    API was down mid-development.
    """
    for name, minimum in (("snum", 700), ("rselected", 90), ("wc", 1000)):
        rows = vocab.load_catalog(name)
        assert isinstance(rows, list)
        assert len(rows) >= minimum, f"{name} has only {len(rows)} records"


def test_catalog_aliases_resolve():
    """sdenom shares snum's vocabulary; cselected/pselected share rselected's."""
    assert vocab.load_catalog("sdenom") is vocab.load_catalog("snum")
    for alias in ("cselected", "pselected"):
        assert vocab.load_catalog(alias) is vocab.load_catalog("rselected")


def test_unknown_catalog_rejected():
    with pytest.raises(ValueError):
        vocab.load_catalog("not_a_catalog")


# ── Search ───────────────────────────────────────────────────────────────

def test_find_attributes_matches_and_filters():
    df = fs.find_attributes("carbon", limit=100)
    assert len(df) > 0
    assert df["description"].str.contains("arbon").any()

    forest_only = fs.find_attributes("carbon", land_basis="Forest land", limit=100)
    assert set(forest_only["land_basis"]) <= {"Forest land"}


def test_find_attributes_surfaces_units_and_eval_type():
    """The two fields that decide whether a query is even valid."""
    df = fs.find_attributes("area of forest land", limit=5)
    assert "units" in df.columns and "eval_typ" in df.columns
    assert df["eval_typ"].notna().all()


def test_find_evaluations_growth_filter():
    """growth_only must actually restrict to growth-accounting evaluations."""
    df = fs.find_evaluations(growth_only=True, limit=200)
    assert len(df) > 0
    assert set(df["growth_acct"].str.upper()) == {"Y"}


def test_describe_grouping_returns_prose_not_html():
    text = fs.describe_grouping("Aspect")
    assert "aspect" in text.lower()
    assert "<" not in text, "PRC_METADATA HTML should be stripped"


def test_describe_grouping_unknown_is_helpful():
    assert "find_groupings" in fs.describe_grouping("no such grouping")


# ── Local validation ─────────────────────────────────────────────────────

def test_validate_rejects_unknown_ids():
    with pytest.raises(fs.FIAValidationError):
        fs.validate(wc=999999, snum=2)
    with pytest.raises(fs.FIAValidationError):
        fs.validate(wc=12025, snum=999999)


def test_validate_catches_growth_attribute_on_nongrowth_evaluation():
    """The pairing check that saves an opaque server-side failure.

    Attributes declare the evaluation type they need; evaluations declare
    whether they support growth accounting. Both facts are local.
    """
    growth = [r for r in vocab.load_catalog("snum")
              if str(r.get("EVAL_TYP", "")).upper() == "EXPGROW"]
    nongrowth = [r for r in vocab.load_catalog("wc")
                 if str(r.get("GROWTH_ACCT", "")).upper() != "Y"]
    if not growth or not nongrowth:
        pytest.skip("no such pairing in the bundled catalogs")

    with pytest.raises(fs.FIAValidationError) as exc:
        fs.validate(wc=nongrowth[0]["EVAL_GRP"], snum=growth[0]["ATTRIBUTE_NBR"])
    assert "growth" in str(exc.value).lower()


def test_bad_forest_definition_rejected_before_network():
    with pytest.raises(fs.FIAValidationError):
        fs.estimate(wc=12025, snum=2, forest_definition="whatever")


# ── Reliability flagging ─────────────────────────────────────────────────

@pytest.mark.parametrize("plots, se, expect_flag, expect_in_reason", [
    (4241, 0.51,  False, ""),
    (258,  6.07,  False, ""),
    (4,    54.92, True,  "4 plots"),
    (1,    99.29, True,  "single plot"),
    (0,    0.0,   True,  "no plots"),
    (None, None,  True,  "no plot count"),
    (500,  45.0,  True,  "standard error"),
])
def test_reliability_reasons(plots, se, expect_flag, expect_in_reason):
    """n=0 and n=1 get their own reasons, not "high error".

    A zero-plot cell reports a zero standard error, which reads as
    *maximum precision* when it means *no information*. Collapsing that
    into the SE test would describe the most dangerous cell in the table
    as merely imprecise.
    """
    reason = fia._reason(se, plots, fia.DEFAULT_MAX_SE_PCT, fia.DEFAULT_MIN_PLOTS)
    assert bool(reason) is expect_flag
    if expect_in_reason:
        assert expect_in_reason in reason


def test_zero_plots_flagged_despite_zero_se():
    """The inversion, stated as its own test because it is the point."""
    assert fia._reason(0.0, 0, 30.0, 30) != ""


# ── Response parsing ─────────────────────────────────────────────────────

_SAMPLE = {
    "EVALIDatorOutput": {
        "numeratorAttributeNumber": 2,
        "numeratorName": "Area of forest land, in acres",
        "FIAorRPAfilter": "RPADEF as the forest land definition.",
        "selectedInventories": {"stateInventory": ["Alabama 012025"]},
        "row": [{
            "content": "Total",
            "column": [
                {"content": "Total", "cellValueNumerator": 22963960.19,
                 "cellSE": 0.5145, "cellPlotNumerator": 4241},
                {"content": "White / red / jack pine group",
                 "cellValueNumerator": 15748.36,
                 "cellSE": 54.9194, "cellPlotNumerator": 4},
            ],
        }],
    }
}


def test_parses_estimate_se_and_plots():
    df = fia._to_frame(_SAMPLE, max_se_pct=30.0, min_plots=30)
    assert len(df) == 2
    assert set(["estimate", "se_pct", "plots", "unreliable",
                "unreliable_reason", "units"]).issubset(df.columns)

    total = df[df["column"] == "Total"].iloc[0]
    assert total["plots"] == 4241 and not total["unreliable"]

    thin = df[df["column"].str.contains("jack pine")].iloc[0]
    assert thin["plots"] == 4 and bool(thin["unreliable"])


def test_parsed_frame_carries_provenance():
    """A saved frame should say which definition produced it."""
    df = fia._to_frame(_SAMPLE, max_se_pct=30.0, min_plots=30)
    assert "RPADEF" in df["forest_definition"].iloc[0]
    assert "Alabama" in df["evaluation"].iloc[0]


def test_reliable_filters_without_mutating():
    df = fia._to_frame(_SAMPLE, max_se_pct=30.0, min_plots=30)
    kept = fs.reliable(df)
    assert len(kept) == 1 and len(df) == 2


def test_unexpected_shape_raises_clearly():
    with pytest.raises(ValueError) as exc:
        fia._to_frame({"something": "else"}, max_se_pct=30.0, min_plots=30)
    assert "EVALIDatorOutput" in str(exc.value)


# ── LCMS envelope handling ───────────────────────────────────────────────

def test_result_envelope_flattens_list_of_lists():
    """``/release/`` returns [[rel], [rel], ...] — one list per release."""
    payload = {"Result": [[{"VersionNumber": "2025-11"}],
                          [{"VersionNumber": "2025-6"}]]}
    out = lcms._result(payload)
    assert len(out) == 2 and out[0]["VersionNumber"] == "2025-11"


def test_result_envelope_leaves_flat_lists_alone():
    """``/summaryareas/`` is already flat and must not be re-flattened."""
    payload = {"Result": [{"Name": "Scott", "Type": "US-COUNTIES"}]}
    out = lcms._result(payload)
    assert len(out) == 1 and out[0]["Name"] == "Scott"


def test_lcms_requires_a_parent_for_child_areas():
    """county needs state, district needs forest — caught locally."""
    with pytest.raises(ValueError):
        fs.lcms_summary(county="Crook")
    with pytest.raises(ValueError):
        fs.lcms_summary(district="Some District")
    with pytest.raises(ValueError):
        fs.lcms_summary()


# ── Error dialects ───────────────────────────────────────────────────────

def test_lcms_inband_error_raises():
    """LCMS answers a bad area with HTTP 200 and ParameterError in body.

    A client trusting the status code returns an empty result instead of
    an error — the most likely source of silent wrong answers here.
    """
    from geeViz.fsInsights._http import UpstreamError, _raise_for_inband_error

    payload = {"Result": {
        "ParameterError": "Invalid Summary Area for LCMS Release 2025-11",
        "ProvidedParameters": {"bbox": "-121,44,-120,45"},
    }}
    with pytest.raises(UpstreamError) as exc:
        _raise_for_inband_error(payload, "http://example.invalid")
    assert "Invalid Summary Area" in str(exc.value)
    assert exc.value.provided.get("bbox")


def test_lcms_valid_payload_passes_through():
    from geeViz.fsInsights._http import _raise_for_inband_error
    _raise_for_inband_error({"Result": {"SummaryArea": "Crook County"}}, "u")


def test_evalidator_error_page_is_recognized():
    """FIADB-API delivers its 500s as an HTML page under HTTP 200.

    Without this, the error reads "expected JSON, got text/html" and
    points at a missing outputFormat — sending the reader to fix
    something that is not broken.
    """
    from geeViz.fsInsights._http import _evalidator_error

    html = ("<html><title>EVALIDator | Error Page</title><body>"
            "<p>Error Type: Internal Server Error</p>"
            "<p>Received an Error: list index out of range</p>"
            "<p>If you used the 'EXPERT ONLY' fields...</p>"
            "<p>API Version: 2.1.7</p></body></html>")
    msg = _evalidator_error(html)
    assert "Internal Server Error" in msg
    assert "list index out of range" in msg


def test_ordinary_html_is_not_mistaken_for_an_evalidator_error():
    from geeViz.fsInsights._http import _evalidator_error
    assert _evalidator_error("<html><body>hello</body></html>") == ""
    assert _evalidator_error("") == ""


def test_runtime_messages_are_ascii():
    """Windows consoles are cp1252; an em-dash in a raised message mangles.

    geeViz has a large Windows user base and these strings are printed
    at exactly the moment something has already gone wrong.
    """
    for plots, se in ((0, 0.0), (1, 99.0), (4, 54.9), (None, None)):
        reason = fia._reason(se, plots, 30.0, 30)
        reason.encode("cp1252")  # raises if non-encodable


# ── Releases are not interchangeable ─────────────────────────────────────

_RELEASES = [
    {"VersionNumber": "2025-11",
     "Products": [{"Name": "Change"}, {"Name": "Land_Cover"},
                  {"Name": "Land_Use"}],
     "StudyAreas": [{"StudyArea": "AK"}, {"StudyArea": "CONUS"}]},
    {"VersionNumber": "2025-6",
     "Products": [{"Name": "NLCD_Percent_Tree_Canopy_Cover"}],
     "StudyAreas": [{"StudyArea": "CONUS"}, {"StudyArea": "AK"}]},
    {"VersionNumber": "2024-10",
     "Products": [{"Name": "Change"}, {"Name": "Land_Cover"},
                  {"Name": "Land_Use"}],
     "StudyAreas": [{"StudyArea": "PRUSVI"}, {"StudyArea": "HAWAII"},
                    {"StudyArea": "AK"}, {"StudyArea": "CONUS"}]},
]


@pytest.fixture
def stub_releases(monkeypatch):
    """Pin the release list so these tests never touch the network."""
    monkeypatch.setitem(lcms._CACHE, "releases", _RELEASES)
    yield
    lcms._CACHE.pop("releases", None)


def test_releases_filter_by_product(stub_releases):
    """2025-6 is a tree-canopy release with no Land_Cover at all."""
    lc = [r["VersionNumber"] for r in lcms.lcms_releases(product="Land_Cover")]
    assert lc == ["2025-11", "2024-10"]

    tcc = [r["VersionNumber"] for r in
           lcms.lcms_releases(product="NLCD_Percent_Tree_Canopy_Cover")]
    assert tcc == ["2025-6"]


def test_latest_release_is_ambiguous_without_a_product(stub_releases):
    """"Latest" differs per product, which is the whole trap.

    2025-6 is newer than 2024-10 but publishes no Land_Cover, so
    resolving "latest" without naming a product can select a release
    that cannot answer the question about to be asked.
    """
    assert lcms.latest_release("Land_Cover") == "2025-11"
    assert lcms.latest_release("NLCD_Percent_Tree_Canopy_Cover") == "2025-6"


def test_latest_release_rejects_unknown_product(stub_releases):
    with pytest.raises(ValueError) as exc:
        lcms.latest_release("Not_A_Product")
    assert "no LCMS release publishes" in str(exc.value)


def test_product_release_mismatch_names_the_fix(stub_releases):
    """The error must name a release that WOULD work.

    Left to the API this surfaces as "Invalid Summary Area", which
    points at the county name and sends the reader to check a spelling
    when the real problem is the release.
    """
    with pytest.raises(ValueError) as exc:
        lcms._check_product("Land_Cover", "2025-6")
    msg = str(exc.value)
    assert "2025-6" in msg and "does not publish" in msg
    assert "2025-11" in msg, "should name a release that carries it"


def test_matching_product_and_release_passes(stub_releases):
    lcms._check_product("Land_Cover", "2025-11")
    lcms._check_product("NLCD_Percent_Tree_Canopy_Cover", "2025-6")


def test_unknown_release_defers_to_the_api(stub_releases):
    """An unrecognized release is not our call to reject."""
    lcms._check_product("Land_Cover", "1999-1")


def test_release_products_lists_what_a_release_carries(stub_releases):
    assert lcms.release_products("2025-6") == ["NLCD_Percent_Tree_Canopy_Cover"]
    assert "Land_Cover" in lcms.release_products("2024-10")


def test_study_areas_are_listed_by_the_names_each_release_uses():
    """The 2025-11 Earth Engine asset covers Hawaii and Puerto Rico through
    2025 (live catalog, 2026-10-09), so nothing should send that work to
    the deprecated 2024-10 and lose 2025. The names change, though: 2025-11
    says HI where 2024-10 said HAWAII, and a filter on the old name
    matches nothing."""
    from geeViz.fsInsights.lcms_ee import RELEASE_STUDY_AREAS

    assert set(RELEASE_STUDY_AREAS["2025-11"]) == {"CONUS", "AK", "HI", "PRUSVI"}
    assert "HAWAII" in RELEASE_STUDY_AREAS["2024-10"]
    assert "HAWAII" not in RELEASE_STUDY_AREAS["2025-11"]


def test_mismatch_message_is_ascii(stub_releases):
    """cp1252 consoles mangle an em-dash in a raised message."""
    with pytest.raises(ValueError) as exc:
        lcms._check_product("Land_Cover", "2025-6")
    str(exc.value).encode("cp1252")


# ── Earth Engine path: study-area mosaicking ─────────────────────────────

class _FakeFiltered:
    def __init__(self, tag): self.tag = tag
    def mosaic(self): return f"mosaic({self.tag})"
    def first(self): return f"first({self.tag})"


class _FakeColl:
    """Stands in for a year-filtered LCMS collection.

    One year holds one image PER STUDY AREA, which is the whole point:
    filtering 2024 in release 2025-11 returns two images, AK and CONUS.
    """
    def __init__(self): self.filtered_with = None
    def filter(self, f):
        self.filtered_with = f
        return _FakeFiltered("AK+CONUS")
    def aggregate_array(self, prop): return self
    def getInfo(self): return [2023, 2024]


def test_year_images_mosaic_rather_than_take_first(monkeypatch):
    """`.first()` picks an arbitrary study area, which was the bug.

    An Oregon geometry against the AK image reduces to nothing, so
    lcms_summary returned an empty frame and the caller's column
    selection raised a bare KeyError. Study areas do not overlap, so
    mosaicking is correct and lets the geometry select its own coverage.
    """
    import types
    fake_ee = types.SimpleNamespace(
        Filter=types.SimpleNamespace(eq=lambda k, v: (k, v)))
    monkeypatch.setitem(sys.modules, "ee", fake_ee)

    coll = _FakeColl()
    out = list(lcms._iter_year_images(coll, [2024]))
    assert out == [(2024, "mosaic(AK+CONUS)")], (
        "must mosaic across study areas, not take .first()")


def test_year_filter_uses_an_int(monkeypatch):
    """`year` is an integer property; the string '2024' matches nothing."""
    import types
    fake_ee = types.SimpleNamespace(
        Filter=types.SimpleNamespace(eq=lambda k, v: (k, v)))
    monkeypatch.setitem(sys.modules, "ee", fake_ee)

    coll = _FakeColl()
    list(lcms._iter_year_images(coll, ["2024"]))
    assert coll.filtered_with == ("year", 2024)
    assert isinstance(coll.filtered_with[1], int)


# ── Empty results keep their column contract ─────────────────────────────

def test_empty_frame_still_has_columns():
    """An empty DataFrame built from [] has NO columns.

    The caller's next line is almost always a column selection, which
    then raises a KeyError naming none of the real problem (an AOI
    outside the release's coverage).
    """
    df = lcms._frame([], columns=lcms._SUMMARY_COLUMNS)
    assert len(df) == 0
    assert list(df.columns) == list(lcms._SUMMARY_COLUMNS)
    df[["year", "class_name", "acres", "source"]]  # must not raise


def test_populated_frame_has_the_same_columns():
    df = lcms._frame([{c: None for c in lcms._SUMMARY_COLUMNS}],
                     columns=lcms._SUMMARY_COLUMNS)
    assert set(lcms._SUMMARY_COLUMNS) <= set(df.columns)


# ── align: class-name validation ─────────────────────────────────────────

_LC_CLASSES = [
    "Trees", "Tall Shrubs (AK Only)", "Tall Shrubs & Trees Mix (AK Only)",
    "Shrubs", "Shrubs & Trees Mix", "Grass/Forb/Herb",
    "Grass/Forb/Herb & Shrubs Mix", "Grass/Forb/Herb & Trees Mix",
    "Barren & Trees Mix", "Barren & Shrubs Mix",
    "Barren & Grass/Forb/Herb Mix", "Barren or Impervious",
    "Snow or Ice", "Water", "Non-Processing Area Mask",
]


@pytest.fixture
def stub_lc_classes(monkeypatch):
    import pandas as pd

    from geeViz.fsInsights import align as _align
    monkeypatch.setattr(
        _align, "lcms_classes",
        lambda product, release="": pd.DataFrame({"class_name": _LC_CLASSES}))


def test_default_tree_classes_are_all_real(stub_lc_classes):
    """Every TREE_CLASSES entry must exist, or it contributes zero acres.

    'Tall Shrubs & Trees Mix' looks correct but the real class carries an
    '(AK Only)' suffix. The wrong name matches nothing, is invisible
    across CONUS where the class never occurs, and quietly understates
    tree area throughout Alaska.
    """
    from geeViz.fsInsights import align as _align
    assert _align._warn_unknown_classes(_align.TREE_CLASSES) == []


def test_the_ak_only_suffix_is_part_of_the_name(stub_lc_classes):
    """Guard the specific mistake, so a future 'tidy-up' cannot reintroduce it."""
    from geeViz.fsInsights import align as _align
    assert "Tall Shrubs & Trees Mix (AK Only)" in _align.TREE_CLASSES
    assert _align._warn_unknown_classes(("Tall Shrubs & Trees Mix",)) == [
        "Tall Shrubs & Trees Mix"]


def test_unknown_class_names_are_reported(stub_lc_classes):
    from geeViz.fsInsights import align as _align
    unknown = _align._warn_unknown_classes(("Trees", "Treez", "Nope"))
    assert unknown == ["Treez", "Nope"]


def test_absent_but_real_class_is_not_flagged(stub_lc_classes):
    """A class real but absent from a county is normal, not an error."""
    from geeViz.fsInsights import align as _align
    assert _align._warn_unknown_classes(("Tall Shrubs (AK Only)",)) == []


def test_class_validation_never_breaks_the_call(monkeypatch):
    """A validation nicety must not take down the real work."""
    from geeViz.fsInsights import align as _align

    def boom(*a, **k):
        raise RuntimeError("network gone")

    monkeypatch.setattr(_align, "lcms_classes", boom)
    assert _align._warn_unknown_classes(("Trees",)) == []


# ── align: caveats travel with the numbers ───────────────────────────────

def test_caveats_available_without_a_live_comparison():
    """Static facts about the datasets, not properties of one call.

    Gating them behind a successful FIA fetch meant the caveats vanished
    exactly when a reader had only one number and might quote it alone.
    """
    from geeViz.fsInsights.align import COMPARISON_CAVEATS

    assert len(COMPARISON_CAVEATS) >= 4
    joined = " ".join(COMPARISON_CAVEATS).lower()
    assert "map accuracy" in joined and "sampling error" in joined
    assert "harvested" in joined, "the definitional difference must be stated"


def test_caveats_are_ascii():
    from geeViz.fsInsights.align import COMPARISON_CAVEATS
    " ".join(COMPARISON_CAVEATS).encode("cp1252")


def test_release_with_no_summary_areas_is_rejected_locally(monkeypatch):
    """A release can publish a product and still answer nothing.

    2022-8 reports SummaryAreaCount=0. Left to the API that surfaces as
    "Invalid Summary Area for LCMS Release 2022-8" -- blaming the county
    name, wrong in exactly the way the product mismatch was.
    """
    rels = [
        {"VersionNumber": "2025-11", "SummaryAreaCount": 3643,
         "Products": [{"Name": "Land_Cover"}]},
        {"VersionNumber": "2022-8", "SummaryAreaCount": 0,
         "Products": [{"Name": "Land_Cover"}]},
    ]
    monkeypatch.setitem(lcms._CACHE, "releases", rels)
    try:
        with pytest.raises(ValueError) as exc:
            lcms._check_product("Land_Cover", "2022-8")
        msg = str(exc.value)
        assert "no summary areas" in msg
        assert "2025-11" in msg, "must name a usable release"
        # A release that DOES have areas still passes.
        lcms._check_product("Land_Cover", "2025-11")
    finally:
        lcms._CACHE.pop("releases", None)


def test_duplicate_attribute_descriptions_are_both_returned():
    """FIA's catalog really does contain duplicates; do not hide them.

    snum 209 and 956 are identical across every field. Collapsing them
    would conceal a real property of the upstream catalog.
    """
    a209, a956 = fs.get_attribute(209), fs.get_attribute(956)
    assert a209 and a956
    assert a209["ATTRIBUTE_DESCR"] == a956["ATTRIBUTE_DESCR"]
    assert a209["EVAL_TYP"] == a956["EVAL_TYP"]

    hits = fs.find_attributes("net growth sawlog", limit=200)
    nums = set(hits["snum"])
    assert {209, 956} <= nums, "both duplicates should be searchable"
