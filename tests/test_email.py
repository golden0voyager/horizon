from email.mime.multipart import MIMEMultipart

from src.models import EmailConfig
from src.services.email import EmailManager


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, server: str, port: int) -> None:
        self.server = server
        self.port = port
        self.login_calls: list[tuple[str, str]] = []
        self.messages: list[MIMEMultipart] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, username, password):
        self.login_calls.append((username, password))

    def send_message(self, message):
        self.messages.append(message)


class FakeIMAP:
    instances: list[tuple[str, int]] = []

    def __init__(self, server: str, port: int) -> None:
        FakeIMAP.instances.append((server, port))


def _email_config(**overrides):
    data = {
        "enabled": True,
        "smtp_server": "smtp.example.com",
        "smtp_port": 465,
        "imap_server": "imap.example.com",
        "imap_port": 993,
        "email_address": "noreply@example.com",
        "password_env": "EMAIL_PASSWORD",
    }
    data.update(overrides)
    return EmailConfig(**data)


def test_send_daily_summary_uses_smtp_username_when_configured(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    config = _email_config(smtp_username="resend")
    manager = EmailManager(config)

    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])

    smtp = FakeSMTP.instances[0]
    assert smtp.login_calls == [("resend", "secret")]
    assert len(smtp.messages) == 1
    assert isinstance(smtp.messages[0], MIMEMultipart)
    assert smtp.messages[0]["From"] == "Horizon Daily <noreply@example.com>"
    assert smtp.messages[0]["To"] == "user@example.com"


def test_send_daily_summary_falls_back_to_email_address_for_smtp_login(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    config = _email_config()
    manager = EmailManager(config)

    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])

    assert FakeSMTP.instances[0].login_calls == [("noreply@example.com", "secret")]


def test_send_daily_summary_escapes_raw_html(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())

    manager.send_daily_summary(
        "# Hello\n\n<img src=x onerror=alert(1)>", "Daily", ["user@example.com"]
    )

    html_part = FakeSMTP.instances[0].messages[0].get_payload()[1]
    html_body = html_part.get_payload(decode=True).decode()
    assert "<h1>Hello</h1>" in html_body
    assert "<img src=x" not in html_body
    assert "&lt;img src=x" in html_body


def test_send_daily_summary_cleans_app_generated_markdown_html(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())
    summary = """# Daily

<a id="item-1"></a>
## Item

<details><summary>参考链接</summary>
<ul>
<li><a href="https://example.com/a">Example A</a></li>
<li><a href="https://example.com/b">Example B</a></li>
</ul>
</details>
"""

    manager.send_daily_summary(summary, "Daily", ["user@example.com"])

    message = FakeSMTP.instances[0].messages[0]
    text_body = message.get_payload()[0].get_payload(decode=True).decode()
    html_body = message.get_payload()[1].get_payload(decode=True).decode()

    assert '<a id="item-1"></a>' not in text_body
    assert "<details>" not in text_body
    assert "<summary>" not in text_body
    assert "**参考链接**" in text_body
    assert "- [Example A](https://example.com/a)" in text_body

    assert '&lt;a id="item-1"&gt;&lt;/a&gt;' not in html_body
    assert "&lt;details&gt;" not in html_body
    assert "&lt;summary&gt;" not in html_body
    assert "<strong>参考链接</strong>" in html_body
    assert '<a href="https://example.com/a">Example A</a>' in html_body
    assert '<a href="https://example.com/b">Example B</a>' in html_body


def test_send_daily_summary_does_not_link_unsafe_details_href(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())
    summary = """# Daily

<details><summary>References</summary>
<ul>
<li><a href="javascript:alert(1)">click [me](https://evil.example)</a></li>
</ul>
</details>
"""

    manager.send_daily_summary(summary, "Daily", ["user@example.com"])

    message = FakeSMTP.instances[0].messages[0]
    text_body = message.get_payload()[0].get_payload(decode=True).decode()
    html_body = message.get_payload()[1].get_payload(decode=True).decode()

    assert 'href="javascript:alert(1)"' not in html_body
    assert "[click](javascript:alert(1))" not in text_body
    assert "- click \\[me\\]\\(https://evil.example\\)" in text_body
    assert "click [me](https://evil.example)" in html_body


def test_check_subscriptions_skips_imap_when_disabled(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeIMAP.instances = []

    config = _email_config(imap_enabled=False)
    manager = EmailManager(config)

    manager.check_subscriptions(storage_manager=object())

    assert FakeIMAP.instances == []


def test_check_subscriptions_skips_when_email_disabled(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeIMAP.instances = []

    config = _email_config(enabled=False)
    manager = EmailManager(config)

    manager.check_subscriptions(storage_manager=object())

    assert FakeIMAP.instances == []


def test_check_subscriptions_adds_new_subscriber(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeSMTP.instances = []

    config = _email_config(imap_enabled=True, subscribe_keyword="SUBSCRIBE")

    class MockIMAP:
        def __init__(self, server, port):
            pass

        def login(self, user, pwd):
            pass

        def select(self, mailbox):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            if "SUBSCRIBE" in criteria:
                return "OK", [b"101"]
            return "OK", [b""]

        def fetch(self, email_id, parts):
            msg_body = (
                b"From: newuser@example.com\r\n"
                b"Subject: SUBSCRIBE\r\n"
                b"\r\n"
            )
            return "OK", [(b"101", msg_body)]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", MockIMAP)

    class FakeStorage:
        def __init__(self):
            self._subs = []

        def load_subscribers(self):
            return list(self._subs)

        def add_subscriber(self, email):
            self._subs.append(email)

        def remove_subscriber(self, email):
            self._subs.remove(email)

    storage = FakeStorage()
    manager = EmailManager(config)
    manager.check_subscriptions(storage_manager=storage)

    assert "newuser@example.com" in storage.load_subscribers()
    assert len(FakeSMTP.instances) == 1
    assert FakeSMTP.instances[0].messages[0]["To"] == "newuser@example.com"


def test_check_subscriptions_skips_noreply_addresses(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeSMTP.instances = []

    config = _email_config(imap_enabled=True, subscribe_keyword="SUBSCRIBE")

    class MockIMAP:
        def __init__(self, server, port):
            pass

        def login(self, user, pwd):
            pass

        def select(self, mailbox):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            if "SUBSCRIBE" in criteria:
                return "OK", [b"101"]
            return "OK", [b""]

        def fetch(self, email_id, parts):
            msg_body = (
                b"From: noreply@spam.com\r\n"
                b"Subject: SUBSCRIBE\r\n"
                b"\r\n"
            )
            return "OK", [(b"101", msg_body)]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", MockIMAP)

    storage = FakeStorage()
    manager = EmailManager(config)
    manager.check_subscriptions(storage_manager=storage)

    assert storage.load_subscribers() == []
    assert FakeSMTP.instances == []


def test_check_subscriptions_removes_unsubscriber(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeSMTP.instances = []

    config = _email_config(
        imap_enabled=True,
        subscribe_keyword="SUBSCRIBE",
        unsubscribe_keyword="UNSUBSCRIBE",
    )

    class FakeStorage:
        def __init__(self):
            self._subs = ["olduser@example.com"]

        def load_subscribers(self):
            return list(self._subs)

        def add_subscriber(self, email):
            self._subs.append(email)

        def remove_subscriber(self, email):
            self._subs.remove(email)

    class MockIMAP:
        def __init__(self, server, port):
            pass

        def login(self, user, pwd):
            pass

        def select(self, mailbox):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            if "UNSUBSCRIBE" in criteria:
                return "OK", [b"202"]
            return "OK", [b""]

        def fetch(self, email_id, parts):
            msg_body = (
                b"From: olduser@example.com\r\n"
                b"Subject: UNSUBSCRIBE\r\n"
                b"\r\n"
            )
            return "OK", [(b"202", msg_body)]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", MockIMAP)

    storage = FakeStorage()
    manager = EmailManager(config)
    manager.check_subscriptions(storage_manager=storage)

    assert "olduser@example.com" not in storage.load_subscribers()
    assert len(FakeSMTP.instances) == 1


def test_check_subscriptions_handles_imap_exception(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")

    class BrokenIMAP:
        def __init__(self, server, port):
            raise ConnectionError("IMAP down")

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", BrokenIMAP)

    config = _email_config(imap_enabled=True)
    manager = EmailManager(config)

    manager.check_subscriptions(storage_manager=object())


def test_check_subscriptions_skips_non_matching_subject(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeSMTP.instances = []

    config = _email_config(imap_enabled=True, subscribe_keyword="SUBSCRIBE")

    class MockIMAP:
        def __init__(self, server, port):
            pass

        def login(self, user, pwd):
            pass

        def select(self, mailbox):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            if "SUBSCRIBE" in criteria:
                return "OK", [b"101"]
            return "OK", [b""]

        def fetch(self, email_id, parts):
            msg_body = (
                b"From: user@example.com\r\n"
                b"Subject: RANDOM\r\n"
                b"\r\n"
            )
            return "OK", [(b"101", msg_body)]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", MockIMAP)

    storage = FakeStorage()
    manager = EmailManager(config)
    manager.check_subscriptions(storage_manager=storage)

    assert storage.load_subscribers() == []


def test_check_subscriptions_skips_already_subscribed(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeSMTP.instances = []

    config = _email_config(imap_enabled=True, subscribe_keyword="SUBSCRIBE")

    class FakeStorage:
        def __init__(self):
            self._subs = ["existing@example.com"]

        def load_subscribers(self):
            return list(self._subs)

        def add_subscriber(self, email):
            self._subs.append(email)

        def remove_subscriber(self, email):
            self._subs.remove(email)

    class MockIMAP:
        def __init__(self, server, port):
            pass

        def login(self, user, pwd):
            pass

        def select(self, mailbox):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            if "SUBSCRIBE" in criteria:
                return "OK", [b"101"]
            return "OK", [b""]

        def fetch(self, email_id, parts):
            msg_body = (
                b"From: existing@example.com\r\n"
                b"Subject: SUBSCRIBE\r\n"
                b"\r\n"
            )
            return "OK", [(b"101", msg_body)]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", MockIMAP)

    storage = FakeStorage()
    manager = EmailManager(config)
    manager.check_subscriptions(storage_manager=storage)

    assert storage.load_subscribers() == ["existing@example.com"]
    assert FakeSMTP.instances == []


def test_check_subscriptions_skips_not_subscribed_unsub(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeSMTP.instances = []

    config = _email_config(
        imap_enabled=True,
        subscribe_keyword="SUBSCRIBE",
        unsubscribe_keyword="UNSUBSCRIBE",
    )

    class FakeStorage:
        def __init__(self):
            self._subs = []

        def load_subscribers(self):
            return list(self._subs)

        def add_subscriber(self, email):
            self._subs.append(email)

        def remove_subscriber(self, email):
            self._subs.remove(email)

    class MockIMAP:
        def __init__(self, server, port):
            pass

        def login(self, user, pwd):
            pass

        def select(self, mailbox):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            if "UNSUBSCRIBE" in criteria:
                return "OK", [b"202"]
            return "OK", [b""]

        def fetch(self, email_id, parts):
            msg_body = (
                b"From: stranger@example.com\r\n"
                b"Subject: UNSUBSCRIBE\r\n"
                b"\r\n"
            )
            return "OK", [(b"202", msg_body)]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", MockIMAP)

    storage = FakeStorage()
    manager = EmailManager(config)
    manager.check_subscriptions(storage_manager=storage)

    assert FakeSMTP.instances == []


def test_check_subscriptions_skips_email_without_at_sign(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeSMTP.instances = []

    config = _email_config(imap_enabled=True, subscribe_keyword="SUBSCRIBE")

    class MockIMAP:
        def __init__(self, server, port):
            pass

        def login(self, user, pwd):
            pass

        def select(self, mailbox):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            if "SUBSCRIBE" in criteria:
                return "OK", [b"101"]
            return "OK", [b""]

        def fetch(self, email_id, parts):
            msg_body = (
                b"From: badsender\r\n"
                b"Subject: SUBSCRIBE\r\n"
                b"\r\n"
            )
            return "OK", [(b"101", msg_body)]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", MockIMAP)

    storage = FakeStorage()
    manager = EmailManager(config)
    manager.check_subscriptions(storage_manager=storage)

    assert storage.load_subscribers() == []


def test_send_daily_summary_skips_when_not_enabled(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    config = _email_config(enabled=False)
    manager = EmailManager(config)

    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])

    assert FakeSMTP.instances == []


def test_send_daily_summary_skips_when_no_subscribers(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())

    manager.send_daily_summary("# Hello", "Daily", [])

    assert FakeSMTP.instances == []


def test_send_daily_summary_sends_to_multiple_subscribers(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())
    subs = ["a@example.com", "b@example.com", "c@example.com"]

    manager.send_daily_summary("# Hello", "Daily", subs)

    smtp = FakeSMTP.instances[0]
    assert len(smtp.messages) == 3
    recipients = [m["To"] for m in smtp.messages]
    assert recipients == subs


def test_send_daily_summary_handles_smtp_exception(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")

    class BrokenSMTP:
        def __init__(self, server, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, pwd):
            raise ConnectionError("SMTP down")

    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", BrokenSMTP)

    manager = EmailManager(_email_config())

    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])


def test_send_daily_summary_handles_individual_send_failure(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")

    class FailOnceSMTP:
        def __init__(self, server, port):
            self.call_count = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, pwd):
            pass

        def send_message(self, msg):
            self.call_count += 1
            if self.call_count == 1:
                raise RuntimeError("Send failed")

    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FailOnceSMTP)

    manager = EmailManager(_email_config())

    manager.send_daily_summary("# Hello", "Daily", ["a@example.com", "b@example.com"])


def test_send_reply_handles_smtp_exception(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")

    class BrokenSMTP:
        def __init__(self, server, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, pwd):
            pass

        def send_message(self, msg):
            raise RuntimeError("Send failed")

    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", BrokenSMTP)

    manager = EmailManager(_email_config())
    manager._send_reply("test@example.com", "Subject", "Body")


def test_send_reply_sends_correct_message(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())
    manager._send_reply("test@example.com", "Subj", "Body text")

    smtp = FakeSMTP.instances[0]
    assert len(smtp.messages) == 1
    msg = smtp.messages[0]
    assert msg["To"] == "test@example.com"
    assert msg["Subject"] == "Subj"


def test_send_daily_summary_uses_fallback_when_markdown_missing(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("src.services.email.markdown", None)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())

    manager.send_daily_summary("Hello", "Daily", ["user@example.com"])

    smtp = FakeSMTP.instances[0]
    html_part = smtp.messages[0].get_payload()[1]
    html_body = html_part.get_payload(decode=True).decode()
    assert "<pre>" in html_body


def test_check_subscriptions_handles_no_sender(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeSMTP.instances = []

    config = _email_config(imap_enabled=True, subscribe_keyword="SUBSCRIBE")

    class MockIMAP:
        def __init__(self, server, port):
            pass

        def login(self, user, pwd):
            pass

        def select(self, mailbox):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            if "SUBSCRIBE" in criteria:
                return "OK", [b"101"]
            return "OK", [b""]

        def fetch(self, email_id, parts):
            msg_body = (
                b"Subject: SUBSCRIBE\r\n"
                b"\r\n"
            )
            return "OK", [(b"101", msg_body)]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", MockIMAP)

    storage = FakeStorage()
    manager = EmailManager(config)
    manager.check_subscriptions(storage_manager=storage)

    assert storage.load_subscribers() == []


def test_check_subscriptions_handles_empty_search_results(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)

    config = _email_config(imap_enabled=True, subscribe_keyword="SUBSCRIBE")

    class MockIMAP:
        def __init__(self, server, port):
            pass

        def login(self, user, pwd):
            pass

        def select(self, mailbox):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            return "OK", [b""]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", MockIMAP)

    manager = EmailManager(config)
    manager.check_subscriptions(storage_manager=object())


def test_init_warns_when_password_env_not_set(monkeypatch):
    monkeypatch.delenv("EMAIL_PASSWORD", raising=False)
    config = _email_config()
    manager = EmailManager(config)
    assert manager.pwd is None


class FakeStorage:
    def __init__(self):
        self._subs = []

    def load_subscribers(self):
        return list(self._subs)

    def add_subscriber(self, email):
        self._subs.append(email)

    def remove_subscriber(self, email):
        self._subs.remove(email)
