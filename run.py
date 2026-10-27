"""Прогон вариантов промпта по набору задач.

Пример:

    python run.py --prompts prompts/support_ticket.yaml --cases cases/support_ticket.yaml

Без ключа можно посмотреть, что будет отправлено: добавьте --dry-run.
"""

import argparse
import asyncio
import functools
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

from core.cache import AnswerCache, cache_key, with_cache
from core.cases import CaseError, load_cases
from core.client import ask_many
from core.pricing import estimate_upper, price_run, usd
from core.providers import ANTHROPIC, GROQ, PROVIDERS, ProviderError, for_model
from core.runner import disagreements, run, save, summarize
from core.variants import VariantError, load_variants

DEFAULT_MODEL = ANTHROPIC.default_model
DEFAULT_CACHE = ".cache/answers"


def load_env(path: str | Path = ".env") -> None:
    """Читает простой файл KEY=VALUE. Уже заданные переменные не трогает.

    Отдельная библиотека ради десяти строк не нужна.
    """
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Сравнение вариантов промпта на наборе задач")
    p.add_argument("--prompts", required=True, help="YAML с вариантами промпта")
    p.add_argument("--cases", required=True, help="YAML с задачами")
    p.add_argument("--model", default=None,
                   help=f"модель (по умолчанию PROMPTDIFF_MODEL, иначе {DEFAULT_MODEL} "
                        f"или {GROQ.default_model}, если задан только GROQ_API_KEY)")
    p.add_argument("--provider", choices=sorted(PROVIDERS), default=None,
                   help="поставщик (по умолчанию PROMPTDIFF_PROVIDER или по имени модели: "
                        "с косой чертой groq, иначе anthropic)")
    p.add_argument("--limit", type=int, default=5, help="сколько запросов одновременно")
    p.add_argument("--rpm", type=float, default=None,
                   help="не больше стольких запросов в минуту (по умолчанию PROMPTDIFF_RPM; "
                        f"для Groq {GROQ.rpm:g}, для Anthropic без ограничения)")
    p.add_argument("--out", default=None, help="куда сохранить результаты (JSON)")
    p.add_argument("--dry-run", action="store_true", help="только показать, что будет отправлено")
    p.add_argument("--no-cache", action="store_true", help="не брать ответы из кэша и не сохранять их туда")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE, help=f"папка кэша ответов (по умолчанию {DEFAULT_CACHE})")
    p.add_argument("--prices", default=None,
                   help="свой файл цен token-counter (по умолчанию TOKEN_COUNTER_PRICES или встроенная таблица)")
    return p.parse_args(argv)


def print_report(results, run_cost=None) -> None:
    print()
    print(f"{'вариант':12} {'прошло':>7} {'нет':>5} {'сбой':>5} {'токены вх/вых':>15} {'сек':>7} {'цена':>11}")
    for s in summarize(results):
        total = s.passed + s.failed + s.errors
        tokens = f"{s.input_tokens}/{s.output_tokens}"
        vc = run_cost.for_variant(s.variant) if run_cost is not None and run_cost.known else None
        price = usd(vc.answers) if vc is not None else "?"
        print(f"{s.variant:12} {s.passed:>3}/{total:<3} {s.failed:>5} {s.errors:>5} {tokens:>15} {s.seconds:>7} {price:>11}")

    if run_cost is not None:
        print()
        if run_cost.known:
            print(f"Цена ответов: {usd(run_cost.answers)}, потрачено в этом запуске: {usd(run_cost.spent)} "
                  f"(цены {run_cost.model} на {run_cost.checked}, token-counter)")
        else:
            print(f"Цена неизвестна: {run_cost.reason}")

    diff = disagreements(results)
    print()
    if not diff:
        print("Расхождений нет: на каждой задаче варианты ответили одинаково по итогу проверок.")
    else:
        print("Расхождения (задача: кто прошёл):")
        for case, marks in diff.items():
            line = ", ".join(f"{v} {'да' if ok else 'нет'}" for v, ok in marks.items())
            print(f"  {case}: {line}")

    cached = sum(r.cached for r in results)
    if cached:
        print()
        print(f"Из кэша: {cached} из {len(results)} ответов, за них в этот раз не платили. "
              "Чтобы спросить модель заново, добавьте --no-cache.")

    errors = [r for r in results if r.error]
    if errors:
        print()
        print("Сбои (ответа нет, промпт тут ни при чём):")
        for r in errors:
            print(f"  {r.variant} / {r.case}: {r.error[:120]}")


def default_model(provider: str | None = None) -> str:
    """Модель, если её не указали в --model.

    Сначала .env: PROMPTDIFF_MODEL или ARENA_MODEL (осталась от старого названия
    проекта prompt-arena, её по-прежнему понимаем, чтобы не сломать заполненные .env).
    Потом модель по умолчанию у выбранного поставщика. Если поставщик не выбран,
    а ключ есть только от Groq, берём модель Groq: так бесплатный прогон
    запускается без лишних флагов.
    """
    from_env = os.environ.get("PROMPTDIFF_MODEL") or os.environ.get("ARENA_MODEL")
    if from_env:
        return from_env
    if provider:
        return PROVIDERS[provider].default_model
    if not os.environ.get(ANTHROPIC.key_env) and os.environ.get(GROQ.key_env):
        return GROQ.default_model
    return DEFAULT_MODEL


def requests_per_minute(arg: float | None, provider) -> float | None:
    """Ограничение частоты: флаг, потом PROMPTDIFF_RPM, потом значение поставщика.
    0 значит без ограничения."""
    if arg is None:
        env = os.environ.get("PROMPTDIFF_RPM")
        if env:
            try:
                arg = float(env)
            except ValueError:
                raise SystemExit(f"PROMPTDIFF_RPM должно быть числом, а не {env!r}") from None
    if arg is None:
        return provider.rpm
    if arg < 0:
        raise SystemExit("--rpm не может быть отрицательным")
    return arg or None


def pace(requests: int, rpm: float) -> str:
    """Сколько минимум займёт прогон при ограничении частоты: первый запрос сразу."""
    seconds = max(0, requests - 1) * 60 / rpm
    return f"{seconds:.0f} с" if seconds < 120 else f"{seconds / 60:.1f} мин"


def main(argv=None) -> int:
    load_env()
    args = parse_args(argv)
    provider_name = args.provider or os.environ.get("PROMPTDIFF_PROVIDER") or None
    try:
        model = args.model or default_model(provider_name)
        provider = for_model(model, provider_name)
    except (ProviderError, KeyError) as err:
        print(f"Неизвестный поставщик: {err}", file=sys.stderr)
        return 2
    rpm = requests_per_minute(args.rpm, provider)

    try:
        variants = load_variants(args.prompts)
        cases = load_cases(args.cases)
    except FileNotFoundError as err:
        print(f"Нет такого файла: {err.filename}", file=sys.stderr)
        return 2
    except yaml.YAMLError as err:
        print(f"Не удалось прочитать YAML: {err}", file=sys.stderr)
        return 2
    except (VariantError, CaseError) as err:
        print(f"Ошибка во входном файле: {err}", file=sys.stderr)
        return 2
    total = len(variants) * len(cases)

    cache = None if args.no_cache else AnswerCache(args.cache_dir)
    print(f"Вариантов: {len(variants)}, задач: {len(cases)}, запросов: {total}, "
          f"модель: {model}, поставщик: {provider.name}")
    # Что реально уйдёт в модель: одинаковые запросы один раз, ответы из кэша не спрашиваем
    to_send = {cache_key(v.prompt, c.input, model, provider=provider): (v.prompt, c.input)
               for v in variants for c in cases}
    if cache is not None:
        total_keys = len(to_send)
        to_send = {k: job for k, job in to_send.items() if cache.get(k) is None}
        print(f"В кэше уже есть ответов: {total_keys - len(to_send)}, пойдёт в модель: {len(to_send)}")

    if args.dry_run:
        for v in variants:
            print(f"\n--- {v.id} ---\n{v.prompt}")
        print(f"\nЗадачи: {', '.join(c.id for c in cases)}")
        # Без кэша одинаковые запросы не склеиваются, в модель уйдёт каждая пара
        jobs = list(to_send.values()) if cache is not None else [(v.prompt, c.input) for v in variants for c in cases]
        upper, reason = estimate_upper(jobs, model, args.prices, max_tokens=provider.max_tokens)
        if upper is None:
            print(f"\nЦена неизвестна: {reason}")
        else:
            print(f"\nПрикидка сверху для {len(jobs)} запросов: ≈{usd(upper)} "
                  "(вход прикинут без токенизатора, каждый ответ посчитан как полный потолок длины)")
        if rpm and jobs:
            print(f"Не больше {rpm:g} запросов в минуту: прогон займёт не меньше {pace(len(jobs), rpm)}")
        return 0

    if not os.environ.get(provider.key_env) and to_send:
        # Проверяем до прогона: иначе получим столько же одинаковых ошибок, сколько запросов.
        # Если все ответы уже в кэше, ключ не нужен: можно перепроверить старые ответы.
        print(f"Не задан {provider.key_env}. Скопируйте .env.example в .env и впишите ключ.", file=sys.stderr)
        return 2

    if rpm and to_send:
        print(f"Не больше {rpm:g} запросов в минуту, это займёт не меньше {pace(len(to_send), rpm)}")
    ask = functools.partial(ask_many, provider=provider, rpm=rpm)
    ask = ask if cache is None else with_cache(ask, cache, provider)
    results = asyncio.run(run(variants, cases, model, limit=args.limit, ask=ask))

    out = args.out or f"results/{datetime.now():%Y-%m-%d_%H-%M}_{Path(args.cases).stem}.json"
    run_cost = price_run(results, model, args.prices)
    save(results, out, {"model": model, "provider": provider.name, "prompts": args.prompts,
                        "cases": args.cases, "cache": cache is not None},
         cost=run_cost.to_json())

    print_report(results, run_cost)
    print(f"\nВсе ответы сохранены в {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
