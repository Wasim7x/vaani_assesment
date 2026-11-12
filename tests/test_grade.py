"""Tests for the deterministic grader."""
from __future__ import annotations

from eval.grade import grade, Question


def _q(**kw):
    defaults = dict(id="x", type="answerable", question="?", paper="attention",
                    section="", key_facts=[], notes="")
    defaults.update(kw)
    return Question(**defaults)


def test_answerable_correct():
    q = _q(key_facts=["512"], paper="attention")
    assert grade("d_model = 512 [attention, §3.1, p.3]", q) == "correct"


def test_answerable_missing_fact_is_wrong():
    q = _q(key_facts=["512"], paper="attention")
    assert grade("Something else [attention, §3.1, p.3]", q) == "wrong"


def test_answerable_wrong_paper_is_wrong():
    q = _q(key_facts=["512"], paper="attention")
    # cited paper is moshi, not attention
    assert grade("d_model = 512 [moshi, §3.1, p.3]", q) == "wrong"


def test_unanswerable_refusal_correct():
    q = _q(type="unanswerable", paper="none")
    assert grade("I cannot answer from the provided papers.", q) == "refused_correctly"


def test_unanswerable_making_stuff_up_wrong():
    q = _q(type="unanswerable", paper="none")
    assert grade("The RPM limit is 999 [attention, §1, p.1]", q) == "wrong"


def test_answerable_refusal_is_wrong_refusal():
    q = _q(key_facts=["512"], paper="attention")
    assert grade("I cannot answer from the provided papers.", q) == "refused_wrongly"
