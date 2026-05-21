from src.cli.main import INTERRUPTED_EXIT_CODE, cli


def test_status_once_exits_successfully(capsys):
    assert cli(["status"]) == 0

    captured = capsys.readouterr()

    assert captured.out == "Checking agent status...\n"
    assert captured.err == ""


def test_status_watch_keyboard_interrupt_returns_documented_code(capsys):
    def interrupt(_interval):
        raise KeyboardInterrupt

    code = cli(["status", "--watch", "--interval", "0.01"], sleep=interrupt)
    captured = capsys.readouterr()

    assert code == INTERRUPTED_EXIT_CODE
    assert captured.out == "Checking agent status...\n"
    assert captured.err == "status watch interrupted\n"


def test_status_watch_repeats_until_interrupt(capsys):
    sleeps = []

    def interrupt_after_second_tick(interval):
        sleeps.append(interval)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    code = cli(
        ["status", "--watch", "--interval", "0.25"],
        sleep=interrupt_after_second_tick,
    )
    captured = capsys.readouterr()

    assert code == INTERRUPTED_EXIT_CODE
    assert sleeps == [0.25, 0.25]
    assert captured.out == "Checking agent status...\n" * 2
    assert captured.err == "status watch interrupted\n"
