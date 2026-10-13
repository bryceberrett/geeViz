"""``now=`` replays one past forecast run, and the agent is told so.

A user asked for "the 2026-09-22 00Z WeatherNext run, hourly leads 0 to
60". Not knowing ``now=`` did that, the agent filtered the raw
collection with ``stringStartsWith('system:index', ...)`` -- a full scan
of the archive, ~30 s per evaluation -- and ten run_code calls timed out
across two turns, ending in a blank answer.

The live test skips rather than fails where Earth Engine (or the gated
WeatherNext asset) is not reachable.
"""
import pathlib

import pytest

INSTRUCTIONS = (pathlib.Path(__file__).resolve().parents[1]
                / "mcp" / "agent-instructions.md")


def test_instructions_name_the_replay_route():
    text = INSTRUCTIONS.read_text(encoding="utf-8")
    assert "One past run, as issued" in text
    assert "pass `now=` that run's init time" in text
    assert "Never pick a run by\n  filtering the raw collection" in text


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


def test_now_replays_a_single_run():
    if not _ee_ready():
        pytest.skip("Earth Engine not reachable")
    import ee
    from geeViz import weather as wx
    ic = wx.getForecastData("2026-09-22T01:00", "2026-09-24T12:00",
                            "weathernext", now="2026-09-22T00:00")
    try:
        leads = ic.aggregate_array("lead_hours").getInfo()
    except ee.EEException as e:
        pytest.skip(f"WeatherNext not reachable: {e}")
    # One run: the leads are consecutive hours from that init, not the
    # lead-1 analyses a past window would otherwise return.
    assert leads == list(range(1, 61))
