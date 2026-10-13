"""GIF and filmstrip frames keep every hour of a sub-daily collection.

``_mosaic_by_date`` groups a collection into one frame per day, which is
right for tiled products (LCMS tiles, Sentinel-2 granules seconds apart
on one pass) and wrong for hourly forecasts. A user asked for a 60-hour
WeatherNext 3 animation of Hurricane Polo and got three frames -- the
last hour of each calendar day, stamped 00:00 -- across four retries.

The wiring test hits Earth Engine on a synthetic collection; it skips
rather than fails where Earth Engine is not reachable.
"""
import datetime

import pytest

HOUR = 3600 * 1000


def _tl():
    # At run time, not collection time: test_esriLib installs a stub
    # geeViz.geeView while the suite is collected, and thumbs reads
    # ``gv.ee`` on import.
    from geeViz.outputLib import thumbs
    return thumbs


def _ms(*args):
    return int(datetime.datetime(*args, tzinfo=datetime.timezone.utc)
               .timestamp() * 1000)


def test_hourly_frames_are_sub_daily():
    t0 = _ms(2026, 9, 22, 6)
    assert _tl()._is_sub_daily([t0 + h * HOUR for h in range(61)])


def test_same_pass_tiles_are_not_sub_daily():
    """Scenes on one pass, seconds apart, still mosaic into one frame."""
    t0 = _ms(2024, 7, 1, 18, 30)
    days = [t0 + d * 86400 * 1000 for d in range(3)]
    tiles = [d + s * 1000 for d in days for s in (0, 24, 48)]
    assert not _tl()._is_sub_daily(tiles)


def test_daily_and_annual_stamps_are_not_sub_daily():
    assert not _tl()._is_sub_daily([_ms(2026, 9, d) for d in range(1, 11)])
    assert not _tl()._is_sub_daily([_ms(y, 1, 1) for y in range(1985, 2026)])
    # Tiles sharing one stamp per year (LCMS) collapse to one per year.
    assert not _tl()._is_sub_daily([_ms(y, 1, 1) for y in (2020, 2020, 2021)])


def test_degenerate_inputs():
    assert not _tl()._is_sub_daily([])
    assert not _tl()._is_sub_daily([None, _ms(2026, 1, 1)])


def _ee_ready():
    try:
        import ee
        try:
            ee.Number(1).getInfo()
            return True
        except Exception:
            from geeViz.geeView import robustInitializer
            robustInitializer()
            ee.Number(1).getInfo()
            return True
    except Exception:
        return False


def test_mosaic_by_date_keeps_each_hour():
    """The wiring: an hourly collection comes out one frame per hour,
    each stamped with its own hour -- not three midnight frames."""
    if not _ee_ready():
        pytest.skip("Earth Engine not reachable")
    import ee
    t0 = _ms(2026, 9, 22, 6)
    stamps = [t0 + h * HOUR for h in range(0, 61, 6)]   # spans 3 days
    col = ee.ImageCollection([
        ee.Image.constant(i).rename("speed").set("system:time_start", t)
        for i, t in enumerate(stamps)])
    out = _tl()._mosaic_by_date(col)
    got = out.aggregate_array("system:time_start").getInfo()
    assert got == stamps
    # And each frame is its own hour's image, not the day's last one.
    vals = out.toList(len(stamps)).map(
        lambda im: ee.Image(im).reduceRegion(
            ee.Reducer.first(), ee.Geometry.Point(0, 0), 1000).get("speed")
    ).getInfo()
    assert vals == list(range(len(stamps)))


def test_hourly_tiles_mosaic_per_hour():
    """Two tiles per hour: still one frame per hour, not per day."""
    if not _ee_ready():
        pytest.skip("Earth Engine not reachable")
    import ee
    t0 = _ms(2026, 9, 22, 22)
    stamps = [t0 + h * HOUR for h in range(4)]          # crosses midnight
    col = ee.ImageCollection([
        ee.Image.constant(1).rename("b").set("system:time_start", t)
        for t in stamps for _ in range(2)])
    out = _tl()._mosaic_by_date(col)
    assert out.aggregate_array("system:time_start").getInfo() == stamps


def test_mosaic_by_date_still_merges_same_pass_tiles():
    if not _ee_ready():
        pytest.skip("Earth Engine not reachable")
    import ee
    t0 = _ms(2024, 7, 1, 18, 30)
    col = ee.ImageCollection([
        ee.Image.constant(1).rename("b").set("system:time_start", t0 + s * 1000)
        for s in (0, 24, 48)])
    assert _tl()._mosaic_by_date(col).size().getInfo() == 1
