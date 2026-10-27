import os

import run as cli
from core.client import Answer


def test_load_env_не_перетирает_заданное(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# комментарий\nA_KEY='из файла'\nB_KEY=из файла\nEMPTY=\nмусор\n", encoding="utf-8")
    monkeypatch.delenv("A_KEY", raising=False)
    monkeypatch.setenv("B_KEY", "уже было")
    monkeypatch.delenv("EMPTY", raising=False)

    cli.load_env(env)

    assert os.environ["A_KEY"] == "из файла"
    assert os.environ["B_KEY"] == "уже было"
    assert "EMPTY" not in os.environ


def test_load_env_без_файла(tmp_path):
    cli.load_env(tmp_path / "нет")


ARGS = ["--prompts", "prompts/support_ticket.yaml", "--cases", "cases/support_ticket.yaml"]


def test_dry_run_без_ключа(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(ARGS + ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "запросов: 14" in out
    assert "--- rules ---" in out


def test_модель_по_умолчанию(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("PROMPTDIFF_MODEL", raising=False)
    monkeypatch.delenv("ARENA_MODEL", raising=False)
    assert cli.default_model() == cli.DEFAULT_MODEL
    # старое имя переменной из .env времён prompt-arena по-прежнему работает
    monkeypatch.setenv("ARENA_MODEL", "старая")
    assert cli.default_model() == "старая"
    # новое имя важнее старого
    monkeypatch.setenv("PROMPTDIFF_MODEL", "новая")
    assert cli.default_model() == "новая"


def test_dry_run_показывает_модель_из_env(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ARENA_MODEL", raising=False)
    monkeypatch.setenv("PROMPTDIFF_MODEL", "claude-haiku-4-5")
    assert cli.main(ARGS + ["--dry-run"]) == 0
    assert "модель: claude-haiku-4-5" in capsys.readouterr().out


def test_без_ключа_понятная_ошибка(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(ARGS) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def fake_model(calls):
    """Подмена ask_many: вариант rules верно понимает «не срочно», short нет."""

    async def ask(jobs, limit, **options):
        # options: поставщик и ограничение частоты, которые run.py передаёт настоящему ask_many
        calls.append(len(jobs))
        fake_model.options = options
        return [
            Answer(
                "order=1204; urgent=no" if "не срочно" in prompt else "order=1204; urgent=yes",
                model, 5, 1, 1, 0.1,
            )
            for prompt, user_input, model in jobs
        ]

    return ask


def test_полный_прогон_с_подменённой_моделью(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    calls = []
    monkeypatch.setattr(cli, "ask_many", fake_model(calls))
    out_file = tmp_path / "run.json"

    assert cli.main(ARGS + ["--out", str(out_file), "--model", "m", "--cache-dir", str(tmp_path / "c")]) == 0
    out = capsys.readouterr().out
    assert out_file.exists()
    assert "Расхождения" in out
    assert "urgent_negated: short нет, rules да" in out
    assert calls == [14]


def test_второй_прогон_из_кэша_без_ключа_и_без_запросов(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    calls = []
    monkeypatch.setattr(cli, "ask_many", fake_model(calls))
    cache = ["--model", "m", "--cache-dir", str(tmp_path / "c")]

    assert cli.main(ARGS + cache + ["--out", str(tmp_path / "1.json")]) == 0
    capsys.readouterr()

    # Ключ убран: все ответы в кэше, поэтому прогон всё равно проходит
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert cli.main(ARGS + cache + ["--out", str(tmp_path / "2.json")]) == 0
    out = capsys.readouterr().out
    assert calls == [14]
    assert "В кэше уже есть ответов: 14, пойдёт в модель: 0" in out
    assert "Из кэша: 14 из 14" in out
    assert "urgent_negated: short нет, rules да" in out


def test_no_cache_спрашивает_заново(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    calls = []
    monkeypatch.setattr(cli, "ask_many", fake_model(calls))
    base = ARGS + ["--model", "m", "--cache-dir", str(tmp_path / "c"), "--out", str(tmp_path / "r.json")]

    assert cli.main(base) == 0
    assert cli.main(base + ["--no-cache"]) == 0
    assert calls == [14, 14]
    assert "Из кэша" not in capsys.readouterr().out.split("запросов: 14")[-1]


def test_dry_run_показывает_сколько_в_кэше(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(ARGS + ["--dry-run", "--cache-dir", str(tmp_path / "пусто")]) == 0
    assert "В кэше уже есть ответов: 0, пойдёт в модель: 14" in capsys.readouterr().out
    assert not (tmp_path / "пусто").exists()


def test_нет_файла_с_промптами_без_трассировки(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    assert cli.main(["--prompts", "нет.yaml", "--cases", "cases/support_ticket.yaml", "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "Нет такого файла: нет.yaml" in err
    assert "Traceback" not in err


def test_битый_yaml_понятная_ошибка(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    bad = tmp_path / "cases.yaml"
    bad.write_text("- id: a\n  input: [не закрыто\n", encoding="utf-8")
    assert cli.main(["--prompts", "prompts/support_ticket.yaml", "--cases", str(bad), "--dry-run"]) == 2
    assert "Не удалось прочитать YAML" in capsys.readouterr().err


def test_ошибка_в_задачах_понятная(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    bad = tmp_path / "cases.yaml"
    bad.write_text("- id: a\n  input: текст\n  expect:\n    exact: yes\n", encoding="utf-8")
    assert cli.main(["--prompts", "prompts/support_ticket.yaml", "--cases", str(bad), "--dry-run"]) == 2
    assert "Ошибка во входном файле: Задача a, exact" in capsys.readouterr().err
