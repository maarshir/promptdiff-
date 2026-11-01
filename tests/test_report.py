import json
from decimal import Decimal

import pytest

import run as cli
from core.checks import check, explain
from core.pricing import RunCost, VariantCost
from core.report import ReportError, build, load_run, to_markdown
from core.runner import Result, save

CASES_YAML = """\
- id: order
  input: заказ 48213
  expect:
    contains_all: ["order=48213"]
- id: urgent
  input: не срочно
  expect:
    contains_all: ["urgent=no"]
    contains_none: ["конечно"]
- id: yes
  input: да или нет
  expect:
    exact: "да"
"""

RESULTS = [
    Result("a", "order", True, answer="order=48213", input_tokens=10, output_tokens=3),
    Result("a", "urgent", True, answer="urgent=no", input_tokens=10, output_tokens=3),
    Result("a", "yes", True, answer="да", input_tokens=10, output_tokens=1),
    Result("b", "order", False, answer="заказ | 48213", input_tokens=12, output_tokens=4),
    Result("b", "urgent", False, answer="конечно, urgent=no", input_tokens=12, output_tokens=4),
    Result("b", "yes", False, error="HTTP 529: перегружен"),
]


@pytest.fixture
def run_json(tmp_path):
    """Прогон, сохранённый тем же save(), что и настоящий."""
    cases = tmp_path / "cases.yaml"
    cases.write_text(CASES_YAML, encoding="utf-8")
    cost = RunCost("модель-x", True, checked="2026-10-01", variants=[
        VariantCost("a", Decimal("0.0012"), Decimal("0.0012")),
        VariantCost("b", Decimal("0.0021"), Decimal("0")),
    ])
    return save(RESULTS, tmp_path / "run.json",
                {"model": "модель-x", "provider": "groq", "cases": str(cases)},
                cost=cost.to_json())


class TestExplain:
    def test_прошёл(self):
        assert explain("order=1", {"contains_all": ["order=1"]}) is None

    def test_чего_не_хватает(self):
        assert explain("order=12", {"contains_all": ["order=1", "x"]}) == "нет «order=1», «x»"

    def test_лишнее(self):
        assert explain("Конечно, да", {"contains_none": ["конечно"]}) == "лишнее «конечно»"

    def test_точно(self):
        assert explain("нет", {"exact": "да"}) == "ожидалось ровно «да»"

    @pytest.mark.parametrize("answer", ["да", "нет", "конечно да", "order=1 да"])
    def test_совпадает_с_check(self, answer):
        expect = {"exact": "да", "contains_all": ["да"], "contains_none": ["конечно"]}
        assert check(answer, expect) == (explain(answer, expect) is None)


def test_таблица_по_вариантам(run_json):
    rows = {r.variant: r for r in build(load_run(run_json)).rows}
    assert (rows["a"].passed, rows["a"].total, rows["a"].failed, rows["a"].errors) == (3, 3, 0, 0)
    assert (rows["b"].passed, rows["b"].failed, rows["b"].errors) == (0, 2, 1)
    assert rows["b"].input_tokens == 24
    assert rows["a"].price != "?"


def test_расхождения_с_причиной(run_json):
    diffs = build(load_run(run_json)).diffs
    # задача со сбоем не расхождение: ответа просто нет
    assert list(diffs) == ["order", "urgent"]
    reasons = {m.variant: m.reason for m in diffs["urgent"]}
    assert reasons == {"a": None, "b": "лишнее «конечно»"}
    assert {m.variant: m.reason for m in diffs["order"]}["b"] == "нет «order=48213»"


def test_без_файла_задач_показывает_ответ(run_json, tmp_path):
    data = load_run(run_json)
    data["meta"]["cases"] = str(tmp_path / "нет.yaml")
    marks = build(data).diffs["order"]
    assert marks[1].reason == "ответ: «заказ | 48213»"


def test_markdown(run_json):
    md = to_markdown(build(load_run(run_json)))
    assert md.startswith("# Отчёт promptdiff")
    assert "| a | 3/3 | 0 | 0 | 30/7 |" in md
    assert "## Расхождения" in md
    assert "- `order`: прошли a; не прошли b (нет «order=48213»)" in md
    assert "## Сбои" in md and "HTTP 529" in md
    # черта из ответа не ломает таблицу и список
    assert "заказ | 48213" not in md


def test_цена_неизвестна(run_json):
    data = load_run(run_json)
    data["cost"] = {"model": "x", "known": False, "reason": "нет такой модели"}
    report = build(data)
    assert all(r.price == "?" for r in report.rows)
    assert "Цена неизвестна: нет такой модели" in to_markdown(report)


def test_старый_json_без_цены_и_лишних_полей(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"results": [
        {"variant": "a", "case": "c", "passed": True, "новое_поле": 1},
        {"variant": "b", "case": "c", "passed": False, "answer": "x"},
    ]}), encoding="utf-8")
    md = to_markdown(build(load_run(path)))
    assert "| a | 1/1 |" in md and "не прошли b (ответ: «x»)" in md


@pytest.mark.parametrize("content", ["не json", "[]", '{"results": [{"variant": "a"}]}'])
def test_не_тот_файл(tmp_path, content):
    path = tmp_path / "x.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ReportError):
        build(load_run(path))


def test_cli_печатает(run_json, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    assert cli.main(["--report", str(run_json)]) == 0
    assert "# Отчёт promptdiff" in capsys.readouterr().out


def test_cli_в_файл(run_json, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    out = tmp_path / "отчёты" / "r.md"
    assert cli.main(["--report", str(run_json), "--report-out", str(out)]) == 0
    assert "## Расхождения" in out.read_text(encoding="utf-8")


def test_cli_нет_файла(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    assert cli.main(["--report", str(tmp_path / "нет.json")]) == 2
    assert "Нет такого файла" in capsys.readouterr().err


def test_cli_без_prompts_и_report(monkeypatch):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    with pytest.raises(SystemExit):
        cli.main(["--cases", "cases/support_ticket.yaml"])
