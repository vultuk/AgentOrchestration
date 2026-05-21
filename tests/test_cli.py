import sys

import pytest

from src.cli.main import cli


def run_cli(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["ao", *args])
    return cli()


def test_logs_tail_rejects_negative_values(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc_info:
        run_cli(monkeypatch, ["logs", "agent-1", "--tail", "-5"])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "must be a non-negative integer" in captured.err


def test_logs_tail_accepts_zero(monkeypatch, capsys):
    run_cli(monkeypatch, ["logs", "agent-1", "--tail", "0"])

    captured = capsys.readouterr()
    assert "Fetching logs for agent: agent-1" in captured.out
    assert captured.err == ""


def test_logs_tail_accepts_positive_values(monkeypatch, capsys):
    run_cli(monkeypatch, ["logs", "agent-1", "--tail", "25"])

    captured = capsys.readouterr()
    assert "Fetching logs for agent: agent-1" in captured.out
    assert captured.err == ""
