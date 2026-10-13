"""run_code pre-checks that misfired in prod on 2026-10-07.

json.loads was refused as unpickling (session fca4b979): the static
check matches any call named ``loads``. The agent was handed a GeoJSON
string, could not parse it, and retyped 23 polygons as a Python literal
instead. ``json.loads`` cannot unpickle; the exemption must still not
let a rebound ``json`` through.

esriLib's module-level draw helpers drew on ``gv.Map``, not the
session's ``Map`` (session 8f659285): the WFIGS perimeters printed
"Adding layer" and were missing from the exported map.
"""
import asyncio
import json
import textwrap

import pytest

import geeViz.mcp.server as srv


@pytest.fixture
def sandboxed(monkeypatch):
    monkeypatch.setattr(srv, "_SANDBOX_ENABLED", True)


def _blocked(code):
    return [w for w in srv._check_code_patterns(textwrap.dedent(code))
            if w.startswith("BLOCKED")]


# ── json.loads ─────────────────────────────────────────────────────────

def test_stdlib_json_loads_is_allowed(sandboxed):
    assert _blocked("""
        import json
        gj = json.loads('{"type": "FeatureCollection", "features": []}')
    """) == []


def test_json_loads_is_allowed_inside_a_function(sandboxed):
    assert _blocked("""
        import json
        def parse(s):
            return json.loads(s)
    """) == []


@pytest.mark.parametrize("code", [
    # no import in this block: `json` may be anything left in the namespace
    "x = json.loads(b)",
    # call before the import
    "x = json.loads(b)\nimport json",
    # rebound by assignment, import alias, from-import, or attribute store
    "import json\njson = pd.compat.pickle_compat\nx = json.loads(b)",
    "import json\nimport pandas.compat.pickle_compat as json\nx = json.loads(b)",
    "import json\nfrom pandas.compat import pickle_compat as json\nx = json.loads(b)",
    "import json\njson.loads = pd.compat.pickle_compat.loads\nx = json.loads(b)",
    "import json\nfor json in [pd.compat.pickle_compat]:\n    json.loads(b)",
    "import json\ndef f(json):\n    return json.loads(b)",
    "import json\nwith ctx() as json:\n    json.loads(b)",
    # other receivers and bare names stay blocked
    "import json\nx = pd.compat.pickle_compat.loads(b)",
    "from json import loads\nx = loads(b)",
    "import pandas as pd\nx = pd.read_pickle('x.pkl')",
])
def test_unsafe_loads_stay_blocked(sandboxed, code):
    assert any("deserializes pickled data" in w for w in _blocked(code)), code


def test_json_loads_exemption_needs_sandbox_off_to_matter(monkeypatch):
    """Outside the sandbox nothing is checked; the exemption is not what
    lets it through there."""
    monkeypatch.setattr(srv, "_SANDBOX_ENABLED", False)
    assert _blocked("x = pd.read_pickle('x')") == []


# ── esriLib helpers off the session map ────────────────────────────────

URL = "https://services3.arcgis.com/x/arcgis/rest/services/P/FeatureServer/0"


@pytest.mark.parametrize("code", [
    f"from geeViz.esriLib import addEsriFeatureService\naddEsriFeatureService({URL!r}, name='P')",
    f"from geeViz.esriLib import addEsriService as add\nadd({URL!r})",
    f"from geeViz import esriLib as el\nel.addEsriMapService({URL!r})",
    f"import geeViz.esriLib as el\nel.addEsriImageService({URL!r})",
    f"import geeViz.esriLib\ngeeViz.esriLib.addEsriFeatureService({URL!r})",
])
def test_module_level_esri_draw_is_refused(code):
    out = srv._esri_off_map_calls(code)
    assert len(out) == 1 and out[0].startswith("BLOCKED"), code
    assert "Map.addEsri" in out[0], "the message must name the call that works"


@pytest.mark.parametrize("code", [
    f"Map.addEsriFeatureService({URL!r}, name='P')",
    f"from geeViz.esriLib import addEsriFeatureService\naddEsriFeatureService({URL!r}, target_map=Map)",
    # georest search/query are not draws
    f"from geeViz.esriLib import queryFeatureService\nqueryFeatureService({URL!r})",
])
def test_session_map_esri_calls_pass(code):
    assert srv._esri_off_map_calls(code) == []


class _Sess:
    def __init__(self, map_ref):
        self._map_ref = map_ref
        self._recent_call_ts = []
        self._breaker_acks = 0


def _run(monkeypatch, sess, code):
    monkeypatch.setattr(srv, "_ensure_initialized", lambda sid=None: sess)
    return json.loads(asyncio.run(srv.run_code(code, session_id="s")))


CODE = f"from geeViz.esriLib import addEsriFeatureService\naddEsriFeatureService({URL!r})"


def test_run_code_refuses_it_for_a_per_session_map(monkeypatch):
    """The wiring: a helper nothing calls protects nothing."""
    out = _run(monkeypatch, _Sess(map_ref=object()), CODE)
    assert out["success"] is False
    assert "Map.addEsriFeatureService" in out["error"]


def test_run_code_leaves_it_alone_when_map_is_gv_map(monkeypatch):
    """The default (local stdio) session's Map IS gv.Map, so the bare
    helper draws where it should; refusing it there would be wrong."""
    import geeViz.geeView as gv
    sess = _Sess(map_ref=gv.Map)
    monkeypatch.setattr(srv, "_ensure_initialized", lambda sid=None: sess)
    # Stop right after the pre-checks: the guard is all that is under test.
    monkeypatch.setattr(srv, "_check_code_patterns",
                        lambda code: ["BLOCKED: sentinel"])
    out = json.loads(asyncio.run(srv.run_code(CODE, session_id="s")))
    assert "Map.addEsriFeatureService" not in out["error"]
    assert "sentinel" in out["error"]
