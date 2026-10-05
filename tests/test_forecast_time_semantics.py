"""``getForecastData`` hides how each model marks run and valid time.

That is the whole point of the function. Three products, three
conventions:

======================  =========================  ====================
model                   run (initialization)       valid time
======================  =========================  ====================
gfs   NOAA/GFS0P25      ``creation_time`` (ms)     ``forecast_time``
euro  ECMWF IFS         ``creation_time`` (ms)     ``forecast_time``
wn    WeatherNext 3     ``start_time`` (ISO str)   ``end_time`` (ISO)
======================  =========================  ====================

Per the WeatherNext schema
(https://developers.google.com/weathernext/guides/earth-engine):
``start_time`` is "the initialization time of the forecast" and
``end_time`` is "the valid time for this specific forecast. Calculated
as start_time + forecast_hour."

Two things make this worth pinning rather than trusting:

* **Earth Engine filters do not coerce types.** Comparing a string
  property against a number returns an EMPTY collection instead of
  raising, so getting it wrong reads as "no data for that window", not
  as a bug. (``ee.Filter.Or`` of both forms does not rescue it — that
  raises ``Cannot compare values ... Type<String> ... Type<Float>``.)
* **WeatherNext's ``system:time_start`` is the INIT time**, one value
  for every image in a run, despite the schema table describing it as
  the valid time. Anything selecting on it picks a run rather than a
  moment. ``getForecastData`` restamps it to the valid time on output so
  time lapses and charts order correctly.

These hit the live collections. They skip rather than fail where Earth
Engine is not reachable, so the suite still runs offline.
"""
import datetime

import pytest


def _ee_ready():
    """Is Earth Engine usable right now?

    Calls ``robustInitializer()`` EXPLICITLY rather than relying on
    ``import geeViz.geeView`` to do it as a side effect. Another test
    module imports geeViz.geeView at module scope, so by the time this
    one is collected the module is already in ``sys.modules`` and a
    second import is a no-op — EE stays uninitialized and every test
    here SKIPS. They passed standalone and skipped in the full suite,
    which is a false pass: a guard test that silently does not run is
    worse than one that fails.
    """
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
    """Decide at RUN time, not collection time.

    ``pytest.mark.skipif(not _ee_ready())`` is evaluated while pytest is
    still collecting, before anything has initialized Earth Engine — so
    every test here skipped in a full-suite run while passing standalone.
    A guard test that silently does not run is a false pass, which is the
    exact failure mode these tests exist to catch elsewhere.
    """
    if not _ee_ready():
        pytest.skip("Earth Engine not reachable")

# Windows are RELATIVE to the current clock, and the clock is the real
# one.
#
# These were pinned to a frozen NOW with absolute dates around it -- a
# "forward" window of 2026-09-12..14 read against now = 2026-09-10. That
# works for exactly as long as the calendar agrees: four days later the
# live archive had moved past those dates, the forward window resolved
# to analyses, and nine tests failed with no code change behind them.
# They were time bombs, and the failure looked like a regression.
#
# Everything here asks the live collections, so the only stable frame of
# reference is the clock they are published against.
NOW = datetime.datetime.now(datetime.timezone.utc).replace(
    minute=0, second=0, microsecond=0)


def _day(offset):
    """A date ``offset`` days from now, as ``YYYY-MM-DD``."""
    return (NOW + datetime.timedelta(days=offset)).strftime("%Y-%m-%d")


# A forward window every model can reach, a past window inside every
# model's retention, and one that straddles the two.
FUT_START, FUT_END = _day(2), _day(4)
PAST_START, PAST_END = _day(-4), _day(-2)
SPAN_START, SPAN_END = _day(-2), _day(3)

MODELS = ["gfs", "euro", "weathernext"]


NO_DATA_LEAD = -1


def _real(d, model):
    """Assert a summary describes DATA, not the empty-window sentinel.

    ``getForecastData`` runs its output through ``fillEmptyCollections``
    so an empty window yields one fully masked u/v image rather than a
    collection that kills ``addWindLayer`` at ``.first().bandNames()``.
    That guard costs something: a sentinel row satisfies ``n > 0``, and
    its ``lead_hours`` of -1 satisfies any ``lead <= 1`` check — so a
    bug that emptied a window would now pass the loose assertions it
    used to fail. Every test asserting real coverage goes through here.
    """
    assert d["n"] > 0, f"{model} returned nothing at all"
    assert d["lead_lo"] != NO_DATA_LEAD, (
        f"{model}: window is the masked no-data sentinel, not forecast "
        f"data — something upstream filtered everything out")


def _summary(ic):
    import ee
    return ee.Dictionary({
        "n": ic.size(),
        "lo": ic.aggregate_min("valid_time"),
        "hi": ic.aggregate_max("valid_time"),
        "lead_lo": ic.aggregate_min("lead_hours"),
        "lead_hi": ic.aggregate_max("lead_hours"),
        "sts_lo": ic.aggregate_min("system:time_start"),
        "sts_hi": ic.aggregate_max("system:time_start"),
        "n_sts": ic.aggregate_count_distinct("system:time_start"),
    }).getInfo()


@pytest.mark.parametrize("model", MODELS)
def test_forward_window_is_covered(model):
    """A future window returns images valid inside it, from one run."""
    import geeViz.weather as wx
    d = _summary(wx.getForecastData(FUT_START, FUT_END, model, now=NOW))
    _real(d, model)
    lo = datetime.datetime.strptime(FUT_START, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc)
    hi = datetime.datetime.strptime(FUT_END, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc)
    assert d["lo"] >= lo.timestamp() * 1000 - 1
    assert d["hi"] <= hi.timestamp() * 1000 + 1
    # Forward means real lead time; a 0-hour lead would be an analysis.
    assert d["lead_lo"] > 0, f"{model} forward window has a zero lead"


@pytest.mark.parametrize("model", MODELS)
def test_system_time_start_is_the_valid_time(model):
    """Restamped on output, for every model.

    WeatherNext otherwise hands back the INIT time — identical across a
    whole run — and a time lapse built on it collapses to one frame.
    """
    import geeViz.weather as wx
    d = _summary(wx.getForecastData(FUT_START, FUT_END, model, now=NOW))
    assert d["sts_lo"] == d["lo"], f"{model}: system:time_start != valid_time"
    assert d["sts_hi"] == d["hi"], f"{model}: system:time_start != valid_time"
    assert d["n_sts"] == d["n"], (
        f"{model}: {d['n_sts']} distinct timestamps for {d['n']} images — "
        f"a run's init time leaked through instead of the valid time")


@pytest.mark.parametrize("model", MODELS)
def test_past_window_is_analyses_only(model):
    """Backwards, every image is the SHORTEST lead its model publishes.

    One fresh analysis per initialization cycle, stitched across runs —
    the best record of what the atmosphere actually did. Not one old run
    projecting across the whole window.

    "Shortest lead" rather than literally 0 because WeatherNext's
    ``forecast_hour`` runs 1..360 and never reaches 0; GFS and ECMWF do
    start at 0. Hard-coding 0 returns an empty collection for
    WeatherNext, which reads as "no data" rather than a wrong constant.
    """
    import geeViz.weather as wx
    d = _summary(wx.getForecastData(PAST_START, PAST_END, model, now=NOW))
    _real(d, model)
    assert d["lead_lo"] == d["lead_hi"], (
        f"{model}: past window mixes leads {d['lead_lo']}..{d['lead_hi']} — "
        f"it should be one lead, the shortest published")
    assert d["lead_hi"] <= 1, (
        f"{model}: past window served at lead {d['lead_hi']}, not an analysis")


@pytest.mark.parametrize("model", MODELS)
def test_forward_window_comes_from_a_single_run(model):
    """One run forward, so the field cannot jump where runs disagree."""
    import ee
    import geeViz.weather as wx
    ic = wx.getForecastData(FUT_START, FUT_END, model, now=NOW)
    # Every image should trace to one initialization: leads increase in
    # step with valid time, so distinct leads == distinct valid times.
    n_lead = ic.aggregate_count_distinct("lead_hours").getInfo()
    n_time = ic.aggregate_count_distinct("valid_time").getInfo()
    assert n_lead == n_time, (
        f"{model}: {n_lead} leads for {n_time} times — more than one run")


@pytest.mark.parametrize("model", MODELS)
def test_the_window_end_is_actually_covered(model):
    """The chosen run must REACH the requested end.

    WeatherNext interleaves 6-hourly inits reaching 360 h with interim
    hourly inits stopping at 48 h, so the most recent initialization is
    often one that cannot span the request. Picking it silently returned
    a window that stopped a day and a half short.
    """
    import datetime as _dt
    import geeViz.weather as wx
    end = _dt.datetime.strptime(FUT_END, "%Y-%m-%d").replace(
        tzinfo=_dt.timezone.utc)
    d = _summary(wx.getForecastData(FUT_START, FUT_END, model, now=NOW))
    reach = _dt.datetime.fromtimestamp(d["hi"] / 1000, _dt.timezone.utc)
    assert (end - reach).total_seconds() <= 6 * 3600, (
        f"{model}: asked to {end:%m-%d %H:%M}, only reached "
        f"{reach:%m-%d %H:%M}")


@pytest.mark.parametrize("model", MODELS)
def test_a_window_opening_now_starts_now(model):
    """Nothing before the requested start.

    "The next 72 hours", asked at 19Z, opens after the newest run began
    (06Z). The run's part was filtered from its own start, not the
    window's, so WeatherNext handed back 85 hourly images for 72 hours -
    twelve already past - and a rain total summed them all
    (LOCAL PATCH forecast window v1, 2026-10-05).
    """
    import geeViz.weather as wx
    start = NOW - datetime.timedelta(hours=1)
    d = _summary(wx.getForecastData(start, start + datetime.timedelta(
        hours=72), model, now=NOW))
    _real(d, model)
    first = datetime.datetime.fromtimestamp(d["lo"] / 1000,
                                            datetime.timezone.utc)
    assert first >= start, (
        f"{model}: asked from {start:%m-%d %H:%M}, got data from "
        f"{first:%m-%d %H:%M}")


@pytest.mark.parametrize("model", MODELS)
def test_spanning_window_joins_analyses_to_a_forecast(model):
    """Past half is analyses, future half is one run, seamed at the most
    recent initialization — so the leads span from the shortest to a
    real forecast horizon."""
    import geeViz.weather as wx
    d = _summary(wx.getForecastData(SPAN_START, SPAN_END, model, now=NOW))
    _real(d, model)
    assert d["lead_lo"] <= 1, (
        f"{model}: spanning window has no analyses (min lead "
        f"{d['lead_lo']})")
    assert d["lead_hi"] > 24, (
        f"{model}: spanning window has no forecast (max lead "
        f"{d['lead_hi']})")


def test_hurricane_helene_2024_is_reachable_in_gfs():
    """The notebook's past example. Only GFS goes back that far: ECMWF
    NRT starts 2024-11-12 and WeatherNext 3 starts in 2026, so the same
    window is legitimately empty for those two."""
    import geeViz.weather as wx
    d = _summary(wx.getForecastData("2024-09-26", "2024-09-27", "gfs",
                                    now=NOW))
    _real(d, "gfs")
    assert d["lead_lo"] == 0 and d["lead_hi"] == 0


def test_weathernext_reaches_days_ahead():
    """It is a forward model, not an archive. The 6-hourly inits carry
    360-hour leads; only the interim hourly inits stop at 48."""
    import geeViz.weather as wx
    d = _summary(wx.getForecastData(_day(2), _day(10),
                                    "weathernext", now=NOW))
    _real(d, "weathernext")
    assert d["lead_hi"] > 48, (
        f"only reached {d['lead_hi']}h — an interim hourly init was chosen "
        f"over a 6-hourly one that covers the window")


def test_declared_time_types_match_the_live_data():
    """Every ``iso_times`` declaration is checked against the collection.

    The flag says whether a model's time properties are ISO 8601 strings
    or epoch millis, and it exists so that building a filter costs no
    round trip. Declaring a static schema fact is only safe if something
    notices when the fact changes — and nothing would notice on its own,
    because a filter with a wrongly typed bound returns an EMPTY
    collection rather than raising. This is that something.

    A failure here means a provider changed representation, and every
    window for that model is about to come back empty.
    """
    import ee
    import geeViz.weather as wx
    for key, spec in wx.MODELS.items():
        first = ee.Image(ee.ImageCollection(spec["collection"]).first())
        for prop in (spec["valid_prop"], spec["run_prop"]):
            live = isinstance(first.get(prop).getInfo(), str)
            assert live == spec["iso_times"], (
                f"{key}.{prop} is "
                f"{'an ISO string' if live else 'epoch millis'} but "
                f"iso_times={spec['iso_times']} — filters on it will "
                f"return empty collections, silently")


def test_no_round_trips_while_building_a_query():
    """``getForecastData`` must not call Earth Engine before it returns.

    It is a graph builder. Every ``getInfo`` it makes is latency the
    caller pays whether or not they ever evaluate the result, and the
    three it used to make (two type probes and a date conversion) cost
    ~0.6s per call for values Python already had or could compute.
    Building the same query now takes single-digit milliseconds.
    """
    import time
    import geeViz.weather as wx
    for model in MODELS:
        t0 = time.time()
        wx.getForecastData(PAST_START, PAST_END, model, now=NOW)
        dt = time.time() - t0
        assert dt < 0.15, (
            f"{model}: building the query took {dt:.2f}s — that is a "
            f"network round trip, not local graph construction")


# ---------------------------------------------------------------------------
# The lead is reduced on the fly, and an empty window is survivable.
# ---------------------------------------------------------------------------


def test_min_lead_is_reduced_server_side():
    """``_min_lead`` returns an ``ee.Number``, not a fetched int.

    It goes straight into ``ee.Filter.eq``, which accepts computed
    values — so the whole window/lead/filter chain is one graph and one
    round trip. Returning a Python int here would mean a ``getInfo``
    per call, on every model, for a value the filter never needed in
    the client.
    """
    import ee
    import geeViz.weather as wx
    ic = ee.ImageCollection("NOAA/GFS0P25").filterDate(PAST_START, PAST_END)
    lead = wx._min_lead(ic, "forecast_hours")
    assert isinstance(lead, ee.Number), (
        f"_min_lead returned {type(lead).__name__}; a client-side value "
        f"means a round trip the filter did not ask for")
    assert lead.getInfo() == 0


def test_min_lead_of_an_empty_collection_is_not_null():
    """``reduceColumns`` gives null for an empty collection, and
    ``ee.Filter.eq(prop, null)`` is an error rather than an empty
    result — so the null has to be absorbed before it reaches a
    filter."""
    import ee
    import geeViz.weather as wx
    empty = ee.ImageCollection("NOAA/GFS0P25").filter(
        ee.Filter.eq("creation_time", -1))
    assert wx._min_lead(empty, "forecast_hours").getInfo() == 0
    # and it is usable as a filter value rather than blowing up
    assert empty.filter(ee.Filter.eq(
        "forecast_hours", wx._min_lead(empty, "forecast_hours"))
    ).size().getInfo() == 0


def test_min_lead_is_not_cached_across_collections():
    """Two different collections, two different answers, no memo.

    The previous implementation cached per collection id for the life of
    the process. A cache that survives a re-init or a switched model is
    a wrong constant waiting to be served, and it bought nothing: the
    reduce is bounded by the window it runs on.
    """
    import geeViz.weather as wx
    assert not any(n.endswith("_CACHE") and "LEAD" in n for n in vars(wx)), (
        "a lead cache is back — the reduce is window-bounded and cheap")


def test_an_empty_window_survives_as_a_masked_sentinel():
    """WeatherNext 3 does not reach back to 2024, so this window is
    legitimately empty. It must still come back with u/v bands.

    Without the guard, ``addWindLayer`` dies several frames later on
    ``Image.bandNames: Parameter 'image' is required`` — which points at
    the renderer rather than at the window that had no data.
    """
    import ee
    import geeViz.weather as wx
    ic = wx.getForecastData("2024-09-26", "2024-09-27", "weathernext",
                            now=NOW)
    d = _summary(ic)
    assert d["n"] == 1, f"expected one sentinel, got {d['n']}"
    assert d["lead_lo"] == NO_DATA_LEAD, (
        "the sentinel must be labelled — lead_hours -1 is how a caller "
        "tells 'no data' from 'calm'")
    assert ee.Image(ic.first()).bandNames().getInfo() == ["u", "v"]
    assert wx.windBands(ee.Image(ic.first()), {}) == ["u", "v"]


@pytest.mark.parametrize("model", MODELS)
def test_the_lead_reduce_is_bounded_by_the_window(model):
    """The min-lead reduce sees the WINDOW, never the whole archive.

    Handing it the unfiltered collection returns the same number for
    these three models — every run publishes its shortest lead — so the
    mistake is invisible in the output and shows up only as latency:
    measured at 275s against 41s for the suite. That is precisely the
    kind of bug a green test suite hides, so the bound is asserted
    structurally rather than trusted.

    The reduce is what replaced a cached ``getInfo``; it is only cheaper
    than the cache it replaced while it stays windowed.
    """
    import ee
    import geeViz.weather as wx
    sizes = []
    real = wx._min_lead

    def spy(coll, lead_p):
        sizes.append(coll.size().getInfo())
        return real(coll, lead_p)

    wx._min_lead = spy
    try:
        wx.getForecastData(PAST_START, PAST_END, model,
                           now=NOW).size().getInfo()
    finally:
        wx._min_lead = real

    assert sizes, f"{model}: the lead was never reduced at all"
    # A two-day window is thousands of images; the archives are millions.
    assert max(sizes) < 20000, (
        f"{model}: reduced over {max(sizes)} images for a two-day window "
        f"— the whole collection reached the reducer instead of the "
        f"windowed slice")


@pytest.mark.parametrize("model", MODELS)
def test_a_spanning_window_has_no_duplicate_instants(model):
    """The two halves must not overlap at the seam.

    A spanning window is analyses BEFORE the newest initialization and
    that run's forecast FROM it. Drop the ``valid_time < seam`` cut and
    both halves cover the hours after the seam: the collection then
    holds two images for the same instant, from different runs. Nothing
    errors — a time lapse just plays each frame twice, with the field
    jumping between two runs' disagreement, which reads as weather.
    """
    import geeViz.weather as wx
    ic = wx.getForecastData(SPAN_START, SPAN_END, model, now=NOW)
    n = ic.size().getInfo()
    distinct = ic.aggregate_count_distinct("valid_time").getInfo()
    assert n == distinct, (
        f"{model}: {n} images for {distinct} distinct instants — the "
        f"analyses and the forecast run overlap past the seam")
