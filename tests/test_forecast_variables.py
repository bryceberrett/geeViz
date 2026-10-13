"""``getForecastData`` is about forecast data, not only about wind.

``variable`` picks what comes back: ``"wind"`` (u/v), a VARIABLES key or
list of them (renamed and unit-normalised), or ``None`` for every band
as published.

The point of the VARIABLES table is that three products spelling the
same quantity three ways -- and two of them publishing Kelvin -- become
one name and one unit. These hit the live collections and skip offline.
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


NOW = datetime.datetime.now(datetime.timezone.utc)


def _day(n):
    return (NOW + datetime.timedelta(days=n)).strftime("%Y-%m-%d")


# A recent window. NOT the head of the collection -- see
# test_the_gfs_band_names_are_checked_against_recent_images.
PAST_A, PAST_B = _day(-3), _day(-1)


def _bands(model, variable):
    import ee
    import geeViz.weather as wx
    ic = wx.getForecastData(PAST_A, PAST_B, model, variable=variable)
    return ee.Image(ic.first()).bandNames().getInfo()


def test_wind_is_the_default_and_still_gives_u_v():
    """``addWindLayer`` and ``downscaleWind`` take the first two bands by
    position, so changing this default would silently feed them whatever
    the product happens to list first."""
    assert _bands("gfs", "wind") == ["u", "v"]
    import geeViz.weather as wx
    import inspect
    sig = inspect.signature(wx.getForecastData)
    assert sig.parameters["variable"].default == "wind"


def test_a_variable_is_renamed_to_its_key():
    """One name across three products that spell it three ways."""
    for model in ("gfs", "euro", "weathernext"):
        assert _bands(model, "temperature_2m") == ["temperature_2m"], model


def test_several_variables_at_once():
    assert _bands("gfs", ["temperature_2m", "precipitation"]) == [
        "temperature_2m", "precipitation"]


def test_none_returns_the_product_as_published():
    """The escape hatch for the hundred-odd WeatherNext bands the table
    does not name."""
    b = _bands("gfs", None)
    assert len(b) > 5
    assert "temperature_2m_above_ground" in b
    assert "temperature_2m" not in b, "None must not rename anything"


def test_kelvin_products_are_converted():
    """WeatherNext publishes Kelvin; GFS and ECMWF publish Celsius.
    Charting them together unconverted puts one line 273 units off the
    others, which reads as a model blow-up rather than a unit mismatch.
    """
    import ee
    import geeViz.weather as wx
    pt = ee.Geometry.Point([-104.99, 39.74])          # Denver
    got = {}
    for m in ("gfs", "euro", "weathernext"):
        img = ee.Image(wx.getForecastData(
            _day(-2), _day(-1), m, variable="temperature_2m").first())
        got[m] = list(img.reduceRegion(
            ee.Reducer.first(), pt, 10000).getInfo().values())[0]
    for m, v in got.items():
        assert -60 < v < 60, (
            f"{m} returned {v:.1f} for a 2 m temperature in Celsius — "
            f"a Kelvin value would land near 300")
    spread = max(got.values()) - min(got.values())
    assert spread < 10, (
        f"the three models disagree by {spread:.1f} C at one point and "
        f"hour; that is a unit problem, not a forecast difference: {got}")


def test_the_0p05_degree_product_is_reachable():
    """It was registered and unusable: nothing in VARIABLES named a band
    of it, so there was no variable to ask for and a plain request was
    rejected for having no wind. Its 12 bands are station-head
    temperature and dewpoint at six statistics each."""
    import geeViz.weather as wx
    assert _bands("weathernext_stations", "temperature_2m") == ["temperature_2m"]
    assert _bands("weathernext_stations", "dewpoint_2m") == ["dewpoint_2m"]
    published = [v for v, e in wx.VARIABLES.items()
                 if e.get("weathernext_stations")]
    assert sorted(published) == ["dewpoint_2m", "temperature_2m"], published


def test_asking_a_model_for_what_it_lacks_names_the_ones_that_have_it():
    """Better than a band-not-found twenty lines later, or an empty
    layer that reads as "no weather"."""
    import geeViz.weather as wx
    with pytest.raises(ValueError, match="publishes no wind"):
        wx.getForecastData(PAST_A, PAST_B, "weathernext_stations",
                           variable="wind")
    with pytest.raises(ValueError, match="does not publish"):
        wx.getForecastData(PAST_A, PAST_B, "gfs",
                           variable="sea_surface_temperature")
    with pytest.raises(ValueError, match="unknown variable"):
        wx.getForecastData(PAST_A, PAST_B, "gfs", variable="not_a_thing")


def test_the_empty_window_sentinel_carries_the_requested_bands():
    """A caller that selects by name must not die on the empty case."""
    import ee
    import geeViz.weather as wx
    # WeatherNext 3 starts in 2024 (MODELS archive_start).
    ic = wx.getForecastData("2023-09-26", "2023-09-27", "weathernext",
                            variable="temperature_2m")
    img = ee.Image(ic.first())
    assert img.bandNames().getInfo() == ["temperature_2m"]
    assert img.get("lead_hours").getInfo() == -1, "not the sentinel"


def test_the_gfs_band_names_are_checked_against_recent_images():
    """GFS's band list is not stable, and the difference is by AGE.

    The oldest images in the collection carry
    ``total_precipitation_surface`` and no dewpoint; every image of the
    last few days carries ``precipitation_rate`` and
    ``dew_point_temperature_2m_above_ground``. An audit that samples
    ``.first()`` or the head of the unfiltered collection reports the
    names in VARIABLES as broken -- they are correct for recent data,
    which is what a forecast request asks for.

    So this samples the window, and every image in it.
    """
    import ee
    import geeViz.weather as wx
    ic = wx.getForecastData(PAST_A, PAST_B, "gfs", variable=None)
    n = ic.size().getInfo()
    assert n > 1
    lst = ic.toList(n)
    common = None
    for i in range(min(n, 6)):
        b = set(ee.Image(lst.get(i)).bandNames().getInfo())
        common = b if common is None else (common & b)
    for var in ("temperature_2m", "precipitation", "dewpoint_2m",
                "relative_humidity_2m", "specific_humidity_2m"):
        name = wx.VARIABLES[var]["gfs"][0]
        assert name in common, (
            f"VARIABLES names {name!r} for gfs {var!r}, but it is not in "
            f"every image of a recent window")
