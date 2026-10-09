import importlib
from pathlib import Path

from lead_parser.application.evaluation import evaluate
from lead_parser.application.models import ClassifiedLead
from lead_parser.bootstrap import evaluation_settings
from lead_parser.core.models import Verdict
from lead_parser.core.policies import decide_classification
from lead_parser.interfaces.cli.evaluate import load_dataset


def test_synthetic_corpus_all_positives_reach_ai_offline(cfg):
    cases = load_dataset()
    report = evaluate(evaluation_settings(cfg), cases)
    assert len(cases) >= 30
    assert report["prefilter"]["recall"] == 1 and report["missed_before_ai"] == []
    assert report["pipeline"] is None and not report["pipeline_complete"]
    assert report["prefilter"]["confusion_matrix"]["fp"] > 0


def test_online_full_pipeline_separates_technical_from_semantic(cfg):
    cases = load_dataset()[:2]

    class PartialClassifier:
        def classify(self, leads):
            return [ClassifiedLead(leads[0], decide_classification(Verdict(False, 0.9, "wrong prediction")))]

    report = evaluate(evaluation_settings(cfg), cases, classifier=PartialClassifier())
    assert len(report["technical_errors"]) == 1 and len(report["semantic_errors"]) == 1
    assert report["pipeline"] is None


def test_service_script_imports_have_no_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = Path(__file__).resolve().parents[1]
    for path in root.glob("*.py"):
        importlib.import_module(path.stem)
    for path in (root / "src" / "lead_parser").rglob("*.py"):
        module = ".".join(path.relative_to(root / "src").with_suffix("").parts)
        importlib.import_module(module.removesuffix(".__init__"))
    assert list(tmp_path.iterdir()) == []
