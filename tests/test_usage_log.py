import copy
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import Mock

import pytest
import requests

from lead_parser.core.models import ClassificationState
from lead_parser.infrastructure.integrations.openrouter import classifier
from lead_parser.infrastructure.integrations.openrouter.classifier import OpenRouterClassifier
from lead_parser.infrastructure.persistence.usage_log import UsageLog, UsageLogError, usage_fields


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _response(*, content=None, cost="0.000123", status_error=None):
    response = Mock()
    response.json.return_value = {
        "id": "gen-123",
        "model": "provider/actual-model",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 12,
            "cost": cost,
            "cost_details": {"upstream_inference_cost": 999},
        },
        "choices": [
            {
                "message": {
                    "content": content
                    or '[{"index":0,"is_client":true,"confidence":0.95,"reason":"request"}]'
                }
            }
        ],
    }
    if status_error is not None:
        response.raise_for_status.side_effect = status_error
    return response


def _configured(cfg, path, *, purpose="pipeline"):
    cfg["ai_filter"].update(enabled=True, openrouter_api_key="secret-api-key")
    return OpenRouterClassifier(cfg, UsageLog(path), purpose=purpose)


def test_paid_batch_has_one_durable_start_and_finish_without_private_data(cfg, lead, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    service = _configured(cfg, path)

    def paid_request(*args, **kwargs):
        records = _records(path)
        assert len(records) == 1 and records[0]["event"] == "started"
        assert path.stat().st_mode & 0o777 == 0o600
        return _response()

    post = Mock(side_effect=paid_request)
    monkeypatch.setattr(classifier.requests, "post", post)
    outcomes = service.classify([lead, copy.deepcopy(lead)])
    assert len(outcomes) == 1 and outcomes[0].decision.state is ClassificationState.ACCEPTED
    post.assert_called_once()
    start, finish = _records(path)
    assert start["event"] == "started" and finish["event"] == "finished"
    assert start["request_id"] == finish["request_id"]
    assert start["started_at"] == finish["started_at"]
    assert start["schema_version"] == 1 and start["provider"] == "openrouter"
    assert start["source_counts"] == {"vk": 1} and start["batch_size"] == 1
    assert start["requested_model"] == cfg["ai_filter"]["model"]
    assert finish["generation_id"] == "gen-123" and finish["model"] == "provider/actual-model"
    assert finish["cost_usd"] == "0.000123"  # Never add upstream BYOK cost automatically.
    assert finish["prompt_tokens"] == 100 and finish["completion_tokens"] == 12
    assert finish["outcome"] == "success" and finish["error_type"] is None
    serialized = path.read_text()
    for secret in (lead.text, lead.phone, lead.external_id, "secret-api-key", "choices", "messages"):
        assert secret not in serialized
    assert "_usage_log" not in cfg


def test_invalid_paid_verdict_still_records_cost(cfg, lead, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(
        classifier.requests, "post", Mock(return_value=_response(content="private bad output"))
    )
    result = _configured(cfg, path).classify([lead])[0]
    assert result.decision.state is ClassificationState.PENDING
    finish = _records(path)[1]
    assert finish["cost_usd"] == "0.000123"
    assert finish["outcome"] == "error" and finish["error_type"] == "InvalidVerdict"
    assert "private bad output" not in path.read_text()


def test_partial_response_is_accounted_once_for_whole_batch(cfg, lead, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    other = copy.deepcopy(lead)
    other.external_id = "-12_10"
    post = Mock(return_value=_response())
    monkeypatch.setattr(classifier.requests, "post", post)
    outcomes = _configured(cfg, path).classify([lead, other])
    assert [value.decision.state for value in outcomes] == [
        ClassificationState.ACCEPTED,
        ClassificationState.PENDING,
    ]
    assert len(_records(path)) == 2 and _records(path)[1]["outcome"] == "partial"
    assert _records(path)[1]["batch_size"] == 2
    post.assert_called_once()


def test_retry_gets_separate_attempt_unknown_timeout_charge(cfg, lead, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    post = Mock(side_effect=[requests.Timeout("secret timeout body"), _response(cost=0)])
    monkeypatch.setattr(classifier.requests, "post", post)
    service = _configured(cfg, path)
    assert service.classify([lead])[0].decision.state is ClassificationState.PENDING
    assert service.classify([lead])[0].decision.state is ClassificationState.ACCEPTED
    records = _records(path)
    assert len(records) == 4 and post.call_count == 2
    assert records[0]["request_id"] != records[2]["request_id"]
    assert records[1]["cost_usd"] is None and records[1]["error_type"] == "Timeout"
    assert records[3]["cost_usd"] == "0"
    assert "secret timeout body" not in path.read_text()


def test_http_error_preserves_returned_accounting(cfg, lead, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    response = _response(status_error=requests.HTTPError("private server reply"))
    monkeypatch.setattr(classifier.requests, "post", Mock(return_value=response))
    result = _configured(cfg, path).classify([lead])[0]
    assert result.decision.state is ClassificationState.PENDING
    finish = _records(path)[1]
    assert finish["error_type"] == "HTTPError" and finish["cost_usd"] == "0.000123"
    assert "private server reply" not in path.read_text()


def test_invalid_response_json_leaves_cost_unknown(cfg, lead, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    response = _response()
    response.json.side_effect = ValueError("private malformed JSON")
    monkeypatch.setattr(classifier.requests, "post", Mock(return_value=response))
    assert _configured(cfg, path).classify([lead])[0].decision.state is ClassificationState.PENDING
    finish = _records(path)[1]
    assert finish["cost_usd"] is None and finish["error_type"] == "ValueError"


def test_start_failure_prevents_paid_http(cfg, lead, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    path.mkdir()  # A directory cannot be used as an append journal.
    post = Mock()
    monkeypatch.setattr(classifier.requests, "post", post)
    result = _configured(cfg, path).classify([lead])[0]
    assert result.decision.state is ClassificationState.PENDING
    assert result.decision.error == "UsageLogError"
    post.assert_not_called()


def test_later_start_failure_preserves_previously_paid_batch(cfg, lead, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    cfg["ai_filter"]["batch_size"] = 1
    service = _configured(cfg, path)
    other = copy.deepcopy(lead)
    other.external_id = "-12_10"
    usage_log = service.cfg["_usage_log"]
    original_append = usage_log._append
    starts = 0

    def fail_second_start(record):
        nonlocal starts
        if record["event"] == "started":
            starts += 1
            if starts == 2:
                raise UsageLogError("Cannot persist second start")
        original_append(record)

    monkeypatch.setattr(usage_log, "_append", fail_second_start)
    post = Mock(return_value=_response())
    monkeypatch.setattr(classifier.requests, "post", post)
    first, second = service.classify([lead, other])
    assert first.decision.state is ClassificationState.ACCEPTED
    assert second.decision.state is ClassificationState.PENDING
    assert second.decision.error == "UsageLogError"
    assert len(_records(path)) == 2
    post.assert_called_once()


def test_finish_failure_keeps_successful_classification(cfg, lead, tmp_path, monkeypatch, caplog):
    path = tmp_path / "ledger.jsonl"
    service = _configured(cfg, path)
    usage_log = service.cfg["_usage_log"]
    original_append = usage_log._append

    def fail_finish(record):
        if record["event"] == "finished":
            raise UsageLogError("private filesystem details")
        original_append(record)

    monkeypatch.setattr(usage_log, "_append", fail_finish)
    post = Mock(return_value=_response())
    monkeypatch.setattr(classifier.requests, "post", post)
    assert service.classify([lead])[0].decision.state is ClassificationState.ACCEPTED
    post.assert_called_once()
    assert len(_records(path)) == 1
    assert _records(path)[0]["request_id"] in caplog.text
    assert "private filesystem details" not in caplog.text


def test_finish_failure_does_not_mask_provider_failure(cfg, lead, tmp_path, monkeypatch, caplog):
    path = tmp_path / "ledger.jsonl"
    service = _configured(cfg, path)
    monkeypatch.setattr(
        service.cfg["_usage_log"], "finish", Mock(side_effect=UsageLogError("private details"))
    )
    monkeypatch.setattr(classifier.requests, "post", Mock(side_effect=requests.Timeout("private body")))
    result = service.classify([lead])[0]
    assert result.decision.state is ClassificationState.PENDING and result.decision.error == "Timeout"
    assert len(_records(path)) == 1 and "private" not in caplog.text


def test_evaluation_is_distinct_and_disabled_ai_makes_no_file(cfg, lead, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    service = _configured(cfg, path, purpose="evaluation")
    cfg["ai_filter"]["enabled"] = False
    assert service.classify([lead])[0].decision.state is ClassificationState.ACCEPTED
    assert not path.exists()
    cfg["ai_filter"]["enabled"] = True
    monkeypatch.setattr(classifier.requests, "post", Mock(return_value=_response()))
    service.classify([lead])
    assert {row["purpose"] for row in _records(path)} == {"evaluation"}


@pytest.mark.parametrize(
    "value", [None, True, False, -1, -0.1, "-1", "nan", "Infinity", {}, [], float("nan")]
)
def test_bad_cost_is_unknown_not_zero(value):
    assert usage_fields({"usage": {"cost": value}})["cost_usd"] is None


@pytest.mark.parametrize("value", [None, True, -1, 1.1, "12", float("nan"), float("inf"), {}])
def test_bad_counters_are_unknown(value):
    fields = usage_fields({"usage": {"prompt_tokens": value, "completion_tokens": value}})
    assert fields["prompt_tokens"] is None and fields["completion_tokens"] is None


def test_absent_fields_and_injected_identifiers_are_not_trusted():
    for data in (None, [], {"usage": []}, {"usage": None}):
        assert all(value is None for value in usage_fields(data).values())
    fields = usage_fields({"id": "prompt text\nprivate", "model": "phone +7999000000"})
    assert fields["generation_id"] is None and fields["model"] is None
    assert usage_fields({"usage": {"cost": Decimal("0.000000123456789")}})["cost_usd"] == "1.23456789E-7"


def _append_attempt(path):
    usage = UsageLog(path)
    request = usage.start(requested_model="model/name", batch_size=1)
    usage.finish(request, response={"usage": {"cost": "0.01"}}, outcome="success", error=None)
    return request["request_id"]


def test_concurrent_processes_do_not_interleave_records(tmp_path):
    path = tmp_path / "nested" / "usage.jsonl"
    with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as pool:
        request_ids = list(pool.map(_append_attempt, [str(path)] * 12))
    records = _records(path)
    assert len(records) == 24 and len(set(request_ids)) == 12
    assert {row["request_id"] for row in records} == set(request_ids)
    assert all(len([row for row in records if row["request_id"] == key]) == 2 for key in request_ids)
    assert path.stat().st_mode & 0o777 == 0o600


def test_shared_logger_is_thread_safe_and_tightens_permissions(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.touch(mode=0o666)
    usage = UsageLog(path)

    def append_one(_):
        return usage.start(requested_model="model/name", batch_size=1)["request_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        request_ids = list(pool.map(append_one, range(16)))
    records = _records(path)
    assert len(records) == 16 and {record["request_id"] for record in records} == set(request_ids)
    assert path.stat().st_mode & 0o777 == 0o600
