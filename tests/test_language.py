from __future__ import annotations

import pytest
from job_search.utils.language import (
    detect_german_ratio,
    detect_language,
    is_predominantly_german,
)


def test_pure_english():
    text = (
        "We are looking for a Senior AI Engineer to join our team in Frankfurt. "
        "You will design, build, and deploy large language model applications. "
        "Requirements: 5+ years of experience with Python, PyTorch, and cloud platforms. "
        "We offer competitive compensation, hybrid working options, and professional development."
    )
    ratio = detect_german_ratio(text)
    assert ratio < 0.10
    assert not is_predominantly_german(text)
    lang, r = detect_language(text)
    assert lang == "en"


def test_english_with_german_snippets():
    text = (
        "Role: AI Solutions Architect at Global Corp. "
        "Standort: Frankfurt am Main. "
        "We are seeking an experienced architect to drive digital transformation initiatives across our enterprise clients. "
        "In this role, you will lead the technical design of generative AI systems, collaborate closely with product teams, "
        "and mentor engineers on modern cloud architectures and machine learning best practices. "
        "You should bring deep hands-on expertise with Python, PyTorch, Kubernetes, and distributed systems. "
        "Strong communication skills are essential for client presentations and technical workshops. "
        "Fluency in English is required; gute Deutschkenntnisse sind von Vorteil. "
        "Bewerben Sie sich jetzt über unser Karriereportal."
    )
    ratio = detect_german_ratio(text)
    # English clearly dominates despite the German location and sentence
    assert ratio < 0.35
    assert not is_predominantly_german(text)
    lang, r = detect_language(text)
    assert lang == "en"


def test_predominantly_german():
    text = (
        "Wir suchen ab sofort eine/n engagierte/n Consultant für AI Transformation (m/w/d). "
        "Deine Aufgaben: Beratung unserer Kunden bei der Konzeption und Umsetzung von KI-Strategien. "
        "Du analysierst bestehende Geschäftsprozesse, identifizierst Optimierungspotenziale durch Machine Learning "
        "und begleitest die Implementierung in agilen Teams. "
        "Wir bieten dir ein dynamisches Arbeitsumfeld, moderne Technologien und ein attraktives Gehaltspaket."
    )
    ratio = detect_german_ratio(text)
    assert ratio > 0.80
    assert is_predominantly_german(text)
    lang, r = detect_language(text)
    assert lang == "de"


def test_empty_or_minimal():
    assert detect_german_ratio("") == 0.0
    assert not is_predominantly_german(None)
    lang, r = detect_language("")
    assert lang == "unknown"
    assert r == 0.0
