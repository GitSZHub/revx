from revx import killswitch, notify


def test_whitelist_halt_alert_names_the_ip(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda t: sent.append(t) or True)
    monkeypatch.setattr(notify, "public_ip", lambda: "203.0.113.7")
    notify.halt_alert("5 consecutive errors, last: HTTP 401: API key can only be used "
                      "for authentication from whitelisted IP")
    assert "203.0.113.7" in sent[0] and "whitelist" in sent[0]


def test_other_halt_alert_carries_the_reason(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda t: sent.append(t) or True)
    notify.halt_alert("floor guard tripped: x")
    assert "floor guard tripped: x" in sent[0]


def test_halt_sends_one_alert(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda t: sent.append(t) or True)
    killswitch.halt(conn, "boom")
    assert len(sent) == 1


def test_send_without_settings_is_a_noop(monkeypatch):
    monkeypatch.undo()
    monkeypatch.delenv("REVX_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("REVX_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    assert notify.send("x") is False
