"""The run leaves the lab as it found it: green, no drift, no faults."""

from __future__ import annotations


def test_lab_is_green_and_clean_at_the_end(lab, green):
    green("final")
    report = lab.drift()
    assert not report.drift and report.findings == [] and report.faults == [] and report.stopped == []
