"""The substitution rule covers inputs that never arrived.

A user asked for a forensic Landsat scene inventory over "the polygons
provided" for two rural properties. None were attached; the agent used a
3 km circle around a point and called it "the property AOI" without a
word. In another session a mask built from VIIRS/FIRMS hotspots was
described as including "interagency perimeter footprints". The general
"say when you substituted" rule existed; neither case read as one.
"""
import pathlib

INSTRUCTIONS = (pathlib.Path(__file__).resolve().parents[1]
                / "mcp" / "agent-instructions.md")


def _rule():
    text = INSTRUCTIONS.read_text(encoding="utf-8")
    start = text.index("**Say when you substituted.**")
    return text[start:text.index("\n", start)]


def test_rule_covers_inputs_that_never_arrived():
    rule = _rule()
    assert "inputs the user refers to that are not in the conversation" in rule
    assert "say they did not arrive" in rule


def test_rule_covers_describing_a_mask_by_its_source():
    assert "Describe every derived layer by what actually built it" in _rule()
