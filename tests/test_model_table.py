"""The documented model facts, checked against the live collections.

``weather.py``'s module docstring carries a table of resolution, archive
start, initialization cadence, horizon and lead step. A table like that
is a set of claims about four feeds nobody here controls, and the
failure mode is not an exception -- it is a user planning a five-day
request against a run that stops in two.

So the numbers live in ``MODELS`` as data, the docstring quotes them,
and this module checks both: the data against Earth Engine, and the
docstring against the data.

The horizon split is the one that matters most. WeatherNext interleaves
6-hourly inits reaching 360 hours with twenty interim hourly inits that
stop at 48, and ECMWF does the same at 360/144. "The most recent run" is
therefore usually one that cannot cover a forward window.
"""
import datetime

import pytest


def _ee_ready():
    try:
        import ee
        try:
            ee.Number(1).getInfo()
            return True
        except Exception:
            # Imported HERE, not at the top: it is only needed when EE is
            # not already up, and importing it eagerly makes this probe
            # fail for an unrelated reason. test_esriLib installs a stub
            # module at sys.modules["geeViz.geeView"] at IMPORT time, and
            # pytest imports every test module during collection before
            # running any -- so the stub is live while earlier files run,
            # and `from geeViz.geeView import robustInitializer` raises
            # "cannot import name ... (unknown location)". That was read
            # as "Earth Engine not reachable" and skipped eleven passing
            # tests silently in every full run.
            from geeViz.geeView import robustInitializer
            robustInitializer()
            ee.Number(1).getInfo()
            return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _require_ee():
    if not _ee_ready():
        pytest.skip("Earth Engine not reachable")


# Static for the same reason as elsewhere: a parametrize list is built at
# COLLECTION time, and importing geeViz.weather pulls in getImagesLib,
# which constructs an ee.ImageCollection at import.
MODEL_KEYS = ["euro", "gfs", "weathernext", "weathernext_stations"]

REQUIRED_FIELDS = ["resolution_deg", "archive_start", "init_interval_h",
                   "horizon_h", "long_run_hours", "lead_step_h", "gated",
                   "ensemble_stats", "native_scale_m"]


def _runs(key, days=2):
    """{init value: [lead, ...]} over a recent window, in one round trip."""
    import ee
    import geeViz.weather as wx
    spec = wx.MODELS[key]
    lo = ee.Date(datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=days)).millis()
    win = (ee.ImageCollection(spec["collection"])
           .filter(ee.Filter.gt("system:time_start", lo)))
    got = win.reduceColumns(ee.Reducer.toList().repeat(2),
                            [spec["run_prop"], spec["lead_prop"]]).getInfo()
    out = {}
    for r, l in zip(*got["list"]):
        out.setdefault(r, []).append(l)
    return out


def _init_hour(run):
    if isinstance(run, str):
        return int(run[11:13])
    return datetime.datetime.fromtimestamp(
        run / 1000, datetime.timezone.utc).hour


def _reach_by_init_hour(runs):
    """Furthest lead each init HOUR is seen to reach.

    A run publishes over a while, so the newest one is usually PARTIAL:
    ECMWF was caught 8.3 hours after its 12:00 init holding 19 of an
    eventual 85 images, leads 0..57 of 360. A horizon read off that is
    not the model's horizon, it is how far the upload had got.

    An age cutoff was the obvious fix and the wrong one -- six hours was
    not enough for ECMWF and any larger number is a guess about someone
    else's publishing pipeline. Taking the MAXIMUM across every run at a
    given init hour needs no such guess: a partial run can only
    under-report, and a 48-hour window holds two runs of each hour, so
    the finished one wins.
    """
    out = {}
    for run, leads in runs.items():
        h = _init_hour(run)
        out[h] = max(out.get(h, 0), max(leads))
    return out


def test_every_model_declares_the_documented_fields():
    """A model added without them renders as blanks in the table, which
    reads as "unknown" when it actually means "nobody looked"."""
    import geeViz.weather as wx
    assert sorted(wx.MODELS) == sorted(MODEL_KEYS)
    for key in MODEL_KEYS:
        missing = [f for f in REQUIRED_FIELDS if f not in wx.MODELS[key]]
        assert not missing, f"MODELS[{key!r}] is missing {missing}"


@pytest.mark.parametrize("key", MODEL_KEYS)
def test_declared_resolution_matches_the_grid(key):
    """``native_scale_m`` feeds areaChartParams and the downscaler's
    'an order of magnitude coarser than the terrain' claim."""
    import ee
    import geeViz.weather as wx
    spec = wx.MODELS[key]
    img = ee.Image(ee.ImageCollection(spec["collection"]).first())
    got = img.projection().nominalScale().getInfo()
    assert abs(got - spec["native_scale_m"]) / spec["native_scale_m"] < 0.02, (
        f"{key}: declared {spec['native_scale_m']} m, grid is {got:.0f} m")
    # Degrees and metres must describe the same grid.
    assert abs(spec["resolution_deg"] * 111319 - got) / got < 0.05, (
        f"{key}: resolution_deg {spec['resolution_deg']} disagrees with "
        f"{got:.0f} m")


@pytest.mark.parametrize("key", MODEL_KEYS)
def test_declared_init_cadence_matches(key):
    import geeViz.weather as wx
    runs = sorted(_runs(key))
    assert len(runs) > 2, f"{key}: only {len(runs)} runs in 48h"
    hours = {_init_hour(r) for r in runs}
    declared = wx.MODELS[key]["init_interval_h"]
    expected = set(range(0, 24, declared))
    assert hours <= expected, (
        f"{key}: declares every {declared}h (so init hours {sorted(expected)}) "
        f"but found inits at {sorted(hours)}")
    assert len(hours) >= min(len(expected), 3), (
        f"{key}: declares every {declared}h but only saw inits at "
        f"{sorted(hours)} in 48 hours")


@pytest.mark.parametrize("key", MODEL_KEYS)
def test_declared_horizons_and_which_runs_reach_them(key):
    """The split, which is the reason getForecastData cannot just take
    the newest run."""
    import geeViz.weather as wx
    spec = wx.MODELS[key]
    runs = _runs(key)
    assert runs, f"{key}: no runs in the last 48h"
    long_h, short_h = spec["horizon_h"]["long"], spec["horizon_h"]["short"]

    reach = _reach_by_init_hour(runs)
    assert reach, f"{key}: no runs to judge"

    seen = set(reach.values())
    assert seen <= {long_h, short_h}, (
        f"{key}: declares horizons {sorted({long_h, short_h})} but init "
        f"hours reach {sorted(seen)} — "
        f"{ {h: m for h, m in sorted(reach.items()) if m not in (long_h, short_h)} }")

    if long_h != short_h:
        long_hours = {h for h, m in reach.items() if m == long_h}
        assert long_hours, (
            f"{key}: no init hour reached the declared long horizon "
            f"{long_h}h; saw {sorted(reach.items())}")
        assert long_hours <= set(spec["long_run_hours"]), (
            f"{key}: declares {long_h}h runs at {spec['long_run_hours']} UTC, "
            f"but found them at {sorted(long_hours)}")
        short_hours = {h for h, m in reach.items() if m == short_h}
        assert not (short_hours & set(spec["long_run_hours"])), (
            f"{key}: an init hour declared long only reached {short_h}h: "
            f"{sorted(short_hours & set(spec['long_run_hours']))}")


@pytest.mark.parametrize("key", MODEL_KEYS)
def test_declared_lead_steps_match(key):
    """``1h → 3h`` on the table means hourly to lead 120, then
    three-hourly. Someone sizing a time lapse reads this to work out how
    many frames they are about to request."""
    import geeViz.weather as wx
    spec = wx.MODELS[key]
    runs = _runs(key)
    # The run that reaches furthest is by construction a COMPLETE one --
    # a partial run cannot out-reach the finished ones at its own init
    # hour, so this never samples a half-published run's spacing.
    longest = max(max(v) for v in runs.values())
    leads = sorted(set(next(v for v in runs.values() if max(v) == longest)))
    declared = spec["lead_step_h"]          # [(from_lead, step), ...]

    def _expected_step(lead):
        step = declared[0][1]
        for start, st in declared:
            if lead >= start:
                step = st
        return step

    for i in range(1, len(leads)):
        gap = leads[i] - leads[i - 1]
        want = _expected_step(leads[i - 1])
        assert gap == want, (
            f"{key}: lead {leads[i-1]}→{leads[i]} is a {gap}h step, but "
            f"lead_step_h {declared} says {want}h")


@pytest.mark.parametrize("key", MODEL_KEYS)
def test_the_archive_really_reaches_back_that_far(key):
    """The table says when data starts. Someone asking for a 2019 case
    study needs to know GFS can answer and the others cannot."""
    import ee
    import geeViz.weather as wx
    spec = wx.MODELS[key]
    first = (ee.ImageCollection(spec["collection"])
             .limit(1, "system:time_start", True)
             .aggregate_array("system:time_start").getInfo())
    assert first, f"{key}: collection appears empty"
    got = datetime.datetime.fromtimestamp(first[0] / 1000,
                                          datetime.timezone.utc).date()
    declared = datetime.datetime.strptime(
        spec["archive_start"], "%Y-%m-%d").date()
    # Within a month: these are rolling feeds and the exact first image
    # can move, but a year of drift means the table is wrong.
    assert abs((got - declared).days) < 32, (
        f"{key}: archive_start says {declared}, earliest image is {got}")


@pytest.mark.parametrize("key", ["weathernext"])  # the models that declare archive_gaps
def test_the_declared_archive_gaps_really_are_empty(key):
    """WeatherNext 3 has 2024 and 2026 on, but no 2025. A gap the table
    declares must hold no images, or the table is telling callers a
    window is empty when it is not."""
    import ee
    import geeViz.weather as wx
    spec = wx.MODELS[key]
    coll = ee.ImageCollection(spec["collection"])
    n = ee.List([coll.filterDate(a, b).limit(1).size() for a, b in spec["archive_gaps"]]).getInfo()
    for (a, b), k in zip(spec["archive_gaps"], n):
        assert k == 0, f"{key}: archive_gaps says {a}..{b} is empty, but it has images"


def test_the_docstring_table_quotes_the_declared_numbers():
    """The table is prose beside data, which is how the two drift.

    Nothing stops someone updating ``MODELS`` and leaving the table
    saying the old thing -- and the table is what a reader trusts.
    """
    import geeViz.weather as wx
    doc = wx.__doc__
    assert doc, "weather.py lost its module docstring"
    for key in MODEL_KEYS:
        spec = wx.MODELS[key]
        deg = f"{spec['resolution_deg']:g}°"
        assert deg in doc, f"{key}: resolution {deg} is not in the table"
        assert spec["archive_start"][:7] in doc, (
            f"{key}: archive {spec['archive_start'][:7]} is not in the table")
        assert f"{spec['horizon_h']['long']}h" in doc, (
            f"{key}: long horizon {spec['horizon_h']['long']}h is not in "
            f"the table")
        if spec["horizon_h"]["short"] != spec["horizon_h"]["long"]:
            assert f"{spec['horizon_h']['short']}h" in doc, (
                f"{key}: short horizon is not in the table, and it is the "
                f"number that surprises people")
        assert f"every {spec['init_interval_h']}h" in doc, (
            f"{key}: init cadence is not in the table")


def test_the_gated_flag_is_honest():
    """A reader uses it to decide whether to build a preflight check."""
    import ee
    import geeViz.weather as wx
    for key in MODEL_KEYS:
        gated = wx.MODELS[key]["gated"]
        assert gated == key.startswith("weathernext"), (
            f"{key}: gated={gated}; only the WeatherNext products are")
        if not gated:
            # An open collection must be readable by anyone, which is
            # the whole claim.
            ee.ImageCollection(wx.MODELS[key]["collection"]).limit(1) \
              .size().getInfo()


def test_ensemble_statistics_are_the_ones_stat_accepts():
    """``stat=`` silently produces a band name; a value not in this list
    yields a select() failure rather than a clear error."""
    import ee
    import geeViz.weather as wx
    for key in MODEL_KEYS:
        stats = wx.MODELS[key]["ensemble_stats"]
        if stats is None:
            continue
        img = ee.Image(ee.ImageCollection(wx.MODELS[key]["collection"]).first())
        bands = set(img.bandNames().getInfo())
        base = wx.VARIABLES["temperature_2m"][key][0]
        for st in stats:
            name = base.replace("_mean", f"_{st}")
            assert name in bands, (
                f"{key}: ensemble_stats lists {st!r} but {name!r} is not a band")
