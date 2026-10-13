"""Separate .getInfo() calls in one run_code block are enforced, not suggested.

Session b2dffb2c (2026-10-09): asked for a classifier's accuracy, the
agent fetched accuracy, kappa, the matrix, producer's and consumer's
accuracy and the band names in separate .getInfo() calls, each its own
server round trip, though the instructions already said to batch them
into one ee.Dictionary. That session also trained a classifier and
reported no accuracy, and added an area breakdown nobody asked for; the
instruction tests below pin the rules written for those.
"""
import re
import textwrap
from pathlib import Path

import geeViz.mcp.server as srv

ROOT = Path(__file__).resolve().parents[1]


def _blocked(code):
    return [w for w in srv._check_code_patterns(textwrap.dedent(code))
            if w.startswith("BLOCKED") and "separate .getInfo()" in w]


# The session's own accuracy step, trimmed to its getInfo calls.
SESSION_ACCURACY = """
    train_cm = classifier.confusionMatrix()
    train_acc = train_cm.accuracy().getInfo()
    train_kappa = train_cm.kappa().getInfo()
    train_matrix = train_cm.getInfo()
    train_producers = train_cm.producersAccuracy().getInfo()
    train_consumers = train_cm.consumersAccuracy().getInfo()
    feat_list = training_samples.getInfo()['features']
    cols = ['class_num'] + band_names.getInfo()
"""


def test_the_sessions_accuracy_step_is_blocked():
    hits = _blocked(SESSION_ACCURACY)
    assert hits, "seven separate round trips must not run"
    assert "7 separate .getInfo() calls" in hits[0]
    assert "ee.Dictionary(" in hits[0], "the message shows the fix"


def test_one_batched_call_is_fine():
    assert _blocked("""
        vals = ee.Dictionary({
            'acc': cm.accuracy(), 'kappa': cm.kappa(),
            'matrix': cm.array(), 'bands': img.bandNames(),
        }).getInfo()
        print(vals['acc'])
    """) == []


def test_up_to_three_are_allowed():
    assert _blocked("""
        a = x.getInfo()
        b = y.getInfo()
        c = z.getInfo()
    """) == []
    assert _blocked("""
        a = x.getInfo(); b = y.getInfo(); c = z.getInfo(); d = w.getInfo()
    """)


def test_comments_and_strings_do_not_count():
    assert _blocked('''
        # x.getInfo() y.getInfo() z.getInfo() w.getInfo()
        note = "call .getInfo() .getInfo() .getInfo() .getInfo()"
        v = ee.Dictionary({'a': 1}).getInfo()
    ''') == []


# ── the instruction rules from the same session ──────────────────────

def _md():
    return (ROOT / "mcp" / "agent-instructions.md").read_text(encoding="utf-8")


def test_unrequested_analysis_is_ruled_out():
    md = _md()
    i = md.index("**Never add outputs the user didn't ask for.**")
    rule = md[i:md.index("\n", i)]
    assert "The same goes for analysis" in rule
    assert "Offer them in one line instead." in rule


def test_trained_models_report_held_out_accuracy():
    md = _md()
    i = md.index("**A trained model comes with its honest accuracy.**")
    rule = md[i:md.index("\n", i)]
    assert "data it was not trained on" in rule
    assert "Accuracy on the training data itself is not accuracy" in rule
    assert "how many samples" in rule
    assert "without waiting to be asked" in rule
    assert "pooled over all held-out predictions" in rule
    # conceptual: no product, band or session specifics in the rule
    assert not re.search(r"smileRandomForest|EMBEDDING|b2dffb2c|LOOCV", rule)
