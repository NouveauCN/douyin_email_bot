from types import SimpleNamespace

import main


def test_main_builds_and_runs_bot_after_loading_config(monkeypatch):
    calls = []
    config = SimpleNamespace(
        email=SimpleNamespace(email="bot@example.test", password="password"),
        douyin=SimpleNamespace(cookie=""),
    )

    class FakeBot:
        def __init__(self, received):
            calls.append(("construct", received))

        def run(self):
            calls.append(("run",))

    monkeypatch.setattr(main, "setup_logging", lambda: calls.append(("logging",)))
    monkeypatch.setattr(main, "load_config", lambda _path: config)
    monkeypatch.setattr(main, "EmailBot", FakeBot)

    main.main()

    assert calls[0] == ("logging",)
    assert calls[1] == ("construct", config)
    assert calls[2] == ("run",)

