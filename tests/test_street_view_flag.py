"""Street View follows one per-tenant setting: instructions and runtime.

AskTerra's Maps keys exclude Street View, yet its agent was told to use
Street View freely, so every request hit a 403 from Google. Now the
tenant setting (GEEVIZ_STREET_VIEW_ENABLED, set from tenant.yaml) both
swaps the instruction rule and makes the gm.streetview_* helpers refuse
before any request.
"""
import re
from pathlib import Path

import pytest

import geeViz.googleMapsLib as gm

ROOT = Path(__file__).resolve().parents[1]
MD = (ROOT / "mcp" / "agent-instructions.md").read_text(encoding="utf-8")


def test_the_shared_rule_is_wrapped_in_markers():
    i, j = MD.index("<!--STREETVIEW_BEGIN-->"), MD.index("<!--STREETVIEW_END-->")
    assert "**Street View — display freely" in MD[i:j]


def test_on_keeps_the_rule_and_drops_the_markers():
    out = gm.street_view_instructions(MD, True)
    assert "**Street View — display freely" in out
    assert "STREETVIEW_" not in out
    assert gm.STREET_VIEW_OFF_RULE not in out


def test_off_replaces_the_rule():
    out = gm.street_view_instructions(MD, False)
    assert "display freely" not in out and "gm.streetview_image(lon, lat" not in out
    assert gm.STREET_VIEW_OFF_RULE in out
    assert "STREETVIEW_" not in out


@pytest.mark.parametrize("fn, args", [
    ("streetview_metadata", (-111.8, 40.6)),
    ("streetview_image", (-111.8, 40.6)),
    ("streetview_images_cardinal", (-111.8, 40.6)),
    ("streetview_panorama", (-111.8, 40.6)),
    ("streetview_html", (-111.8, 40.6)),
])
def test_every_street_view_helper_refuses_when_off(monkeypatch, fn, args):
    monkeypatch.setenv("GEEVIZ_STREET_VIEW_ENABLED", "0")
    # no request may be made: fail loudly if one is attempted
    monkeypatch.setattr(gm, "_fetch_json", lambda *a, **k: pytest.fail("made a request"))
    with pytest.raises(RuntimeError, match="Street View .* is not available here"):
        getattr(gm, fn)(*args)


def test_absent_means_on(monkeypatch):
    monkeypatch.delenv("GEEVIZ_STREET_VIEW_ENABLED", raising=False)
    assert gm.street_view_enabled() is True
    for v in ("0", "false", "No", "off"):
        monkeypatch.setenv("GEEVIZ_STREET_VIEW_ENABLED", v)
        assert gm.street_view_enabled() is False


def test_the_mcp_server_applies_it():
    src = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "street_view_instructions(_SERVER_INSTRUCTIONS, street_view_enabled())" in code
