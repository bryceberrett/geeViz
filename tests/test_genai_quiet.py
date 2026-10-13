"""geeViz's Gemini calls must not fill run_code's stderr with log noise.

A successful interpret_image call showed three log lines in the MCP
tool's stderr (2026-10-09): google-genai's "Both GOOGLE_API_KEY and
GEMINI_API_KEY are set" warning, its "AFC is enabled" info and warning,
and httpx's "HTTP Request: POST ... 200 OK". None was an error, and all
read like one.
"""
import logging
import re
from pathlib import Path

from geeViz import llmUsage as lu

ROOT = Path(__file__).resolve().parents[1]


def _code(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


def test_the_stripper_removes_comments():
    assert "genai.Client(" not in "\n".join(
        l for l in "# genai.Client(\nx = 1".splitlines() if not l.lstrip().startswith("#"))


def test_the_config_turns_automatic_function_calling_off():
    cfg = lu.genai_config(temperature=0.0, response_mime_type="application/json")
    assert cfg.automatic_function_calling.disable is True
    assert cfg.temperature == 0.0 and cfg.response_mime_type == "application/json"


def test_the_filter_drops_only_the_both_keys_warning():
    f = lu._DropKeyWarning()
    mk = lambda msg: logging.LogRecord("google_genai._api_client", logging.WARNING, "", 0, msg, None, None)
    assert f.filter(mk("Both GOOGLE_API_KEY and GEMINI_API_KEY are set. Using GOOGLE_API_KEY.")) is False
    assert f.filter(mk("Some real problem")) is True


def test_the_client_installs_the_filter_once():
    lg = logging.getLogger("google_genai._api_client")
    lu.genai_client("test-key")
    lu.genai_client("test-key")
    assert lg.filters.count(lu._key_filter) == 1


def test_every_gemini_call_site_uses_the_quiet_helpers():
    for path in ("googleMapsLib.py", "inventoryLib.py", "outputLib/reports.py"):
        code = _code(path)
        assert "genai.Client(" not in code, f"{path} builds a client directly"
        assert "GenerateContentConfig(" not in code, f"{path} builds a config directly"
        assert "config={" not in code, f"{path} passes a raw config dict"
        assert "_lu.genai_client(" in code


def test_the_mcp_server_keeps_httpx_at_warning():
    code = _code("mcp/server.py")
    assert 'for _noisy in ("httpx", "httpcore"):' in code
    assert "_logging.getLogger(_noisy).setLevel(_logging.WARNING)" in code
