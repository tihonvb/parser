from dataclasses import FrozenInstanceError

import pytest

from lead_parser.core.models import ClassificationDecision, ClassificationState, Lead, Verdict
from lead_parser.core.policies import (
    decide_classification,
    extract_phone,
    keyword_decision,
    normalize_text,
    unique_leads,
)


def test_source_is_part_of_lead_identity():
    vk = Lead(source="vk", external_id="123", date="")
    telegram = Lead(source="telegram", external_id="123", date="")
    assert vk.dedupe_key() != telegram.dedupe_key()
    with pytest.raises(ValueError):
        Lead(source="vk", external_id="  ", date="")
    provider = Lead(source="new_provider", external_id="123", date="")
    assert provider.dedupe_key() == "new_provider:123"
    for invalid in ("", "vk:12", "VK", "with space"):
        with pytest.raises(ValueError):
            Lead(source=invalid, external_id="123", date="")


def test_merge_preserves_first_identity_order_and_enriches_without_aliasing_callers():
    first = Lead(source="vk", external_id="123", date="", text="Ищу мастера", extra={"first": [1]})
    other = Lead(source="telegram", external_id="123", date="", text="Ищу мастера")
    richer = Lead(
        source="vk",
        external_id="123",
        date="2026-10-09T10:00:00+00:00",
        text="Ищу мастера для ремонта квартиры",
        phone="+79990000000",
        source_group_id="vk:10",
        extra={"first": [2], "later": {"nested": [3]}},
    )
    merged = unique_leads(iter([first, other, richer]))
    assert [lead.dedupe_key() for lead in merged] == ["vk:123", "telegram:123"]
    assert merged[0].text == richer.text
    assert merged[0].phone == richer.phone
    assert merged[0].date == richer.date
    assert merged[0].source_group_id == richer.source_group_id
    assert merged[0].extra == {"first": [1], "later": {"nested": [3]}}
    merged[0].extra["first"].append(8)
    merged[0].extra["later"]["nested"].append(9)
    assert first.text == "Ищу мастера" and first.phone == ""
    assert first.extra == {"first": [1]}
    assert richer.extra == {"first": [2], "later": {"nested": [3]}}


def test_merge_does_not_overwrite_existing_metadata_or_equal_length_text():
    first = Lead(source="vk", external_id="123", date="", text="ABC", phone="first")
    later = Lead(source="vk", external_id="123", date="", text="XYZ", phone="later")
    assert unique_leads([first, later])[0].text == "ABC"
    assert unique_leads([first, later])[0].phone == "first"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (" \nНУЖЕН\u00a0РЕМОНТ\t ", (True, "configured_phrase")),
        ("Посоветуйте хорошего мастера", (True, "request_and_topic")),
        ("Нужен ремонт. Продаю квартиру", (False, "excluded_phrase")),
        ("Услуги ремонта квартир", (False, "no_request_or_keyword")),
        (" \t", (False, "empty")),
    ],
)
def test_candidate_rules_preserve_reason_and_exclusion_precedence(text, expected):
    assert keyword_decision(text, ["нужен ремонт"], ["продаю квартиру"]) == expected


def test_normalization_and_phone_extraction():
    assert normalize_text("ВСЁ\u00a0ГОТОВО\n") == "все готово"
    assert extract_phone("Звонить 8 (999) 123-45-67") == "+79991234567"
    assert extract_phone("Идентификатор 1799912345672") is None


@pytest.mark.parametrize(
    ("is_client", "confidence", "expected"),
    [
        (True, 0.75, ClassificationState.ACCEPTED),
        (True, 0.749, ClassificationState.REJECTED),
        (False, 1.0, ClassificationState.REJECTED),
    ],
)
def test_qualification_requires_client_intent_and_threshold(is_client, confidence, expected):
    verdict = Verdict(is_client=is_client, confidence=confidence, reason="Assessment")
    decision = decide_classification(verdict, min_confidence=0.75)
    assert decision.state is expected
    assert decision.verdict is verdict
    with pytest.raises(FrozenInstanceError):
        verdict.confidence = 0.0
    with pytest.raises(FrozenInstanceError):
        decision.state = ClassificationState.PENDING


@pytest.mark.parametrize("confidence", [True, "0.9", float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_confidence_cannot_become_a_domain_verdict(confidence):
    with pytest.raises(ValueError):
        Verdict(is_client=True, confidence=confidence, reason="Assessment")


def test_technical_failure_cannot_become_rejection():
    pending = ClassificationDecision(state=ClassificationState.PENDING, error="ProviderUnavailable")
    assert pending.verdict is None
    with pytest.raises(ValueError):
        ClassificationDecision(state=ClassificationState.REJECTED, error="ProviderUnavailable")
    with pytest.raises(ValueError):
        ClassificationDecision(state=ClassificationState.REJECTED)
    with pytest.raises(ValueError):
        ClassificationDecision(state=ClassificationState.ACCEPTED, verdict=Verdict(False, 1, "An advert"))
    assert ClassificationDecision(ClassificationState.ACCEPTED).verdict is None
