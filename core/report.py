"""Отчёт по сохранённому прогону: итоги по вариантам и задачи, где варианты разошлись.

Читает JSON, который пишет core.runner.save, и ничего не спрашивает у модели,
поэтому ключ не нужен. Причину провала берёт из проверки задачи, если файл
задач из прогона (meta.cases) ещё на месте; иначе показывает начало ответа.
"""

import json
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path

from core.cases import CaseError, load_cases
from core.checks import explain
from core.pricing import usd
from core.runner import Result, disagreements, summarize

EXCERPT = 80


class ReportError(ValueError):
    pass


@dataclass
class Row:
    variant: str
    passed: int
    total: int
    failed: int
    errors: int
    input_tokens: int
    output_tokens: int
    price: str  # "?" если цена неизвестна


@dataclass
class Mark:
    variant: str
    passed: bool
    reason: str | None = None


@dataclass
class Report:
    meta: dict
    rows: list[Row]
    cost_note: str | None
    diffs: dict[str, list[Mark]]
    errors: list[tuple[str, str, str]] = field(default_factory=list)  # вариант, задача, сбой


def load_run(path: str | Path) -> dict:
    """Читает JSON прогона и проверяет, что это он, а не случайный файл."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReportError(f"Нет такого файла: {path}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ReportError(f"{path}: не JSON ({err})") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ReportError(f"{path}: нет списка results, это не файл прогона promptdiff")
    return data


def _results(data: dict) -> list[Result]:
    known = {f.name for f in fields(Result)}
    out = []
    for i, r in enumerate(data["results"]):
        if not isinstance(r, dict) or not {"variant", "case", "passed"} <= r.keys():
            raise ReportError(f"Результат {i}: нужны поля variant, case и passed")
        out.append(Result(**{k: v for k, v in r.items() if k in known}))
    return out


def _expectations(data: dict, cases_path: str | Path | None) -> dict[str, dict]:
    """Ожидания задач по id. Нет файла или он сломан: пусто, отчёт обойдётся без них."""
    path = cases_path or data.get("meta", {}).get("cases")
    if not path:
        return {}
    try:
        return {c.id: c.expect for c in load_cases(path)}
    except (OSError, CaseError, ValueError):
        return {}


def _excerpt(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= EXCERPT else text[: EXCERPT - 1] + "…"


def _reason(result: Result, expect: dict | None) -> str:
    if expect is not None and result.answer is not None:
        why = explain(result.answer, expect)
        if why:
            return why
    return f"ответ: «{_excerpt(result.answer)}»"


def _prices(data: dict) -> tuple[dict[str, str], str | None]:
    cost = data.get("cost")
    if not isinstance(cost, dict):
        return {}, None
    if not cost.get("known"):
        return {}, f"Цена неизвестна: {cost.get('reason') or 'нет цены модели'}"
    prices = {}
    for v in cost.get("variants", []):
        try:
            prices[v["variant"]] = usd(Decimal(v["answers_usd"]))
        except (KeyError, TypeError, InvalidOperation):
            continue
    note = None
    try:
        note = (f"Цена ответов: {usd(Decimal(cost['answers_usd']))} "
                f"(цены {cost.get('model')} на {cost.get('prices_checked')}, token-counter)")
    except (KeyError, TypeError, InvalidOperation):
        pass
    return prices, note


def build(data: dict, cases_path: str | Path | None = None) -> Report:
    """Разбор прогона, общий для всех форматов отчёта."""
    results = _results(data)
    prices, cost_note = _prices(data)
    rows = [
        Row(s.variant, s.passed, s.passed + s.failed + s.errors, s.failed, s.errors,
            s.input_tokens, s.output_tokens, prices.get(s.variant, "?"))
        for s in summarize(results)
    ]

    expects = _expectations(data, cases_path)
    by_pair = {(r.variant, r.case): r for r in results}
    diffs = {}
    for case, marks in disagreements(results).items():
        diffs[case] = [
            Mark(v, ok, None if ok else _reason(by_pair[(v, case)], expects.get(case)))
            for v, ok in marks.items()
        ]

    errors = [(r.variant, r.case, _excerpt(r.error)) for r in results if r.error]
    return Report(data.get("meta", {}), rows, cost_note, diffs, errors)


def _md(text) -> str:
    """Текст для ячейки таблицы Markdown: без переводов строк и с экранированной чертой."""
    return " ".join(str(text).split()).replace("|", "\\|")


def to_markdown(report: Report) -> str:
    meta = report.meta
    lines = ["# Отчёт promptdiff", ""]
    about = [f"модель {meta['model']}" if meta.get("model") else None,
             f"поставщик {meta['provider']}" if meta.get("provider") else None,
             f"задачи {meta['cases']}" if meta.get("cases") else None,
             f"сохранён {meta['saved_at']}" if meta.get("saved_at") else None]
    about = ", ".join(a for a in about if a)
    if about:
        lines += [_md(about[0].upper() + about[1:]), ""]

    lines += ["| вариант | прошло | нет | сбой | токены вх/вых | цена |",
              "|---|---:|---:|---:|---:|---:|"]
    for r in report.rows:
        lines.append(f"| {_md(r.variant)} | {r.passed}/{r.total} | {r.failed} | {r.errors} "
                     f"| {r.input_tokens}/{r.output_tokens} | {r.price} |")
    if report.cost_note:
        lines += ["", _md(report.cost_note)]

    lines += ["", "## Расхождения", ""]
    if not report.diffs:
        lines.append("Нет: на каждой задаче варианты ответили одинаково по итогу проверок.")
    for case, marks in report.diffs.items():
        ok = ", ".join(_md(m.variant) for m in marks if m.passed)
        bad = "; ".join(f"{_md(m.variant)} ({_md(m.reason)})" for m in marks if not m.passed)
        lines.append(f"- `{_md(case)}`: прошли {ok}; не прошли {bad}")

    if report.errors:
        lines += ["", "## Сбои", "", "Ответа нет, промпт тут ни при чём.", ""]
        lines += [f"- {_md(v)} / `{_md(c)}`: {_md(e)}" for v, c, e in report.errors]

    return "\n".join(lines) + "\n"
