import importlib
from pathlib import Path

import ai_filter
from evaluate import evaluate, load_dataset


def test_synthetic_corpus_all_positives_reach_ai_offline(cfg):
    cases = load_dataset()
    report = evaluate(cfg, cases)
    assert len(cases) >= 30
    assert report["prefilter"]["recall"] == 1 and report["missed_before_ai"] == []
    assert report["pipeline"] is None and not report["pipeline_complete"]
    assert report["prefilter"]["confusion_matrix"]["fp"] > 0


def test_online_full_pipeline_separates_technical_from_semantic(cfg, monkeypatch):
    cfg["ai_filter"].update(enabled=True, openrouter_api_key="test")
    cases = load_dataset()[:2]
    monkeypatch.setattr(
        ai_filter,
        "_classify_batch",
        lambda *args: {0: {"index": 0, "is_client": False, "confidence": 0.9, "reason": "wrong prediction"}},
    )
    report = evaluate(cfg, cases, online=True)
    assert len(report["technical_errors"]) == 1 and len(report["semantic_errors"]) == 1
    assert report["pipeline"] is None


def test_service_script_imports_have_no_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for path in Path(__file__).resolve().parents[1].glob("*.py"):
        importlib.import_module(path.stem)
    assert list(tmp_path.iterdir()) == []
