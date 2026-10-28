"""Exercise app/window wiring with deterministic GTK timers and WebKit replies."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("gi")

from doubao_murmur import app as app_module, transcription
from doubao_murmur.app_state import LoginStatus, RecordingState
from doubao_murmur.config import SESSION_NOTICE_DURATION
from doubao_murmur.params_store import ASRParams, ParamsStore
from doubao_murmur.session_status import SessionFault
from doubao_murmur.ui import login_window as browser
from tests.test_transcription import FakeGLib


class Signals:
    def __init__(self):
        self.handlers = {}

    def connect(self, name, callback):
        self.handlers[name] = callback

    def emit(self, name, *args):
        return self.handlers[name](self, *args)


class Window(Signals):
    def __init__(self):
        super().__init__()
        self.visible = False
        self.destroyed = False

    def set_title(self, title):
        pass

    def set_default_size(self, *size):
        pass

    def set_child(self, child):
        self.child = child

    def present(self):
        self.visible = True

    def get_visible(self):
        return self.visible

    def set_visible(self, visible):
        self.visible = visible

    def destroy(self):
        self.destroyed = True


class WebView(Signals):
    def __init__(self, *, user_content_manager):
        super().__init__()
        self.messages = user_content_manager
        self.cookies = []
        self.scripts = []
        self.clears = []
        self.loads = []
        self.manager = SimpleNamespace(
            get_cookie_manager=lambda: self,
            get_website_data_manager=lambda: self,
        )

    def set_settings(self, settings):
        pass

    def get_network_session(self):
        return self.manager

    def get_cookies(self, uri, cancellable, callback, data):
        self.cookies.append(callback)

    def get_cookies_finish(self, result):
        if isinstance(result, Exception):
            raise result
        return result

    def evaluate_javascript(self, *args):
        self.scripts.append(args[-2])

    def evaluate_javascript_finish(self, result):
        if isinstance(result, Exception):
            raise result
        return result

    def clear(self, *args):
        self.clears.append(args[-2])

    def clear_finish(self, result):
        pass

    def load_uri(self, uri):
        self.loads.append(uri)

    def stop_loading(self):
        pass

    def login(self):
        self.messages.emit("script-message-received::asr_handler", {
            "type": "login", "status": "loggedIn", "nickname": "tester",
        })

    def reply_cookies(self):
        cookie = SimpleNamespace(
            get_domain=lambda: ".doubao.com",
            get_name=lambda: "sessionid", get_value=lambda: "test-only",
        )
        self.cookies.pop(0)(self, [cookie])

    def reply_params(self, data=None):
        if data is None:
            data = {"tea_cache_raw": json.dumps({"web_id": 123})}
        value = SimpleNamespace(to_string=lambda: json.dumps(data))
        self.scripts.pop(0)(self, value)


@pytest.fixture
def session(monkeypatch, tmp_path):
    glib = FakeGLib()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(browser, "GLib", glib)
    monkeypatch.setattr(browser, "_HAS_WEBKIT", True)
    monkeypatch.setattr(browser, "WebKit", SimpleNamespace(
        Settings=Mock(), UserContentManager=Signals, WebView=WebView,
        WebsiteDataTypes=SimpleNamespace(ALL=255),
        LoadEvent=SimpleNamespace(FINISHED="finished"),
        PolicyDecisionType=SimpleNamespace(NAVIGATION_ACTION="navigation"),
    ), raising=False)
    monkeypatch.setattr(Signals, "register_script_message_handler", Mock(), raising=False)
    monkeypatch.setattr(browser.LoginWindow, "_load_js_resource", lambda *args: None)
    monkeypatch.setattr(browser.Gtk, "Window", Window)
    monkeypatch.setattr(app_module.Gtk, "MessageDialog", Mock())
    app = app_module.DoubaoMurmurApp()
    app.app_state.login_status = LoginStatus.NOT_LOGGED_IN
    app._show_login()
    return app, glib


def complete_login(app, glib):
    view = app.login_window._webview
    view.login()
    glib.advance(1000)
    view.reply_cookies()
    view.reply_params()


def test_success_waits_for_extraction_and_persistence(session, monkeypatch):
    app, glib = session
    login = app.login_window
    window, view = login._window, login._webview
    statuses = []
    app.app_state.connect("login-status-changed", lambda _, value: statuses.append(value))
    save = Mock(wraps=ParamsStore.save)
    monkeypatch.setattr(app_module.ParamsStore, "save", save)
    view.login()
    glib.advance(999)
    assert not view.cookies
    assert app.app_state.login_status == LoginStatus.NOT_LOGGED_IN
    glib.advance(1)
    view.reply_cookies()
    assert not save.called
    view.reply_params()

    save.assert_called_once()
    assert ParamsStore.load().device_id == "123"
    assert statuses == [LoginStatus.LOGGED_IN.value]
    assert window.destroyed
    assert app.login_window is None
    assert not login.is_active


@pytest.mark.parametrize("failure", [
    "cookie", "javascript", "cookie_start", "javascript_start",
    "missing_ids", "save_false", "save_exception",
])
def test_failure_keeps_browser_and_allows_retry(session, monkeypatch, failure):
    app, glib = session
    login, view = app.login_window, app.login_window._webview
    original_save = ParamsStore.save
    original_get_cookies = view.get_cookies
    original_evaluate = view.evaluate_javascript
    if failure == "cookie_start":
        view.get_cookies = Mock(side_effect=RuntimeError("cookie request failed"))
    if failure == "javascript_start":
        view.evaluate_javascript = Mock(side_effect=RuntimeError("JS request failed"))
    if failure.startswith("save"):
        monkeypatch.setattr(ParamsStore, "save", Mock(
            return_value=False,
            side_effect=OSError("read-only") if failure == "save_exception" else None,
        ))
    view.login()
    glib.advance(1000)
    if failure == "cookie_start":
        pass
    elif failure == "cookie":
        view.cookies.pop(0)(view, RuntimeError("cookies unavailable"))
    else:
        view.reply_cookies()
        if failure == "javascript_start":
            pass
        elif failure == "javascript":
            view.scripts.pop(0)(view, RuntimeError("script failed"))
        else:
            view.reply_params({} if failure == "missing_ids" else None)

    assert app.login_window is login and login.is_active
    assert login._window.get_visible()
    assert app.app_state.login_status == LoginStatus.NOT_LOGGED_IN
    assert app.app_state.error_message
    app_module.Gtk.MessageDialog.return_value.present.assert_called_once()
    assert ParamsStore.load() is None
    monkeypatch.setattr(ParamsStore, "save", original_save)
    view.get_cookies = original_get_cookies
    view.evaluate_javascript = original_evaluate
    complete_login(app, glib)
    assert app.app_state.login_status == LoginStatus.LOGGED_IN
    assert app.app_state.error_message is None


def test_duplicate_notifications_extract_and_save_once(session, monkeypatch):
    app, glib = session
    view = app.login_window._webview
    save = Mock(wraps=ParamsStore.save)
    monkeypatch.setattr(ParamsStore, "save", save)
    view.login()
    view.login()
    glib.advance(1000)
    assert len(view.cookies) == 1
    view.login()
    view.reply_cookies()
    duplicate_js = view.scripts[0]
    view.reply_params()
    duplicate_js(view, None)
    view.login()
    glib.advance(5000)
    save.assert_called_once()


@pytest.mark.parametrize("stage", ["delay", "cookies", "javascript"])
@pytest.mark.parametrize("action", ["logout", "replace", "reopen_same"])
def test_stale_extraction_cannot_save_or_close_new_browser(session, stage, action):
    app, glib = session
    old_login, old_view = app.login_window, app.login_window._webview
    old_view.login()
    if stage != "delay":
        glib.advance(1000)
    if stage == "javascript":
        old_view.reply_cookies()
    if action == "logout":
        app._do_logout()
        new_view = app.login_window._webview
        assert len(new_view.clears) == 1
        assert not new_view.loads
        new_view.clears.pop()(new_view, object())
        assert new_view.loads
    else:
        old_login.destroy()
        if action == "reopen_same":
            app.login_window = old_login
        app._show_login()
    replacement = app.login_window
    if stage == "cookies":
        old_view.reply_cookies()
        assert not old_view.scripts
    elif stage == "javascript":
        old_view.reply_params()
    old_view.login()
    glib.advance(5000)

    assert ParamsStore.load() is None
    assert app.app_state.login_status == LoginStatus.NOT_LOGGED_IN
    app_module.Gtk.MessageDialog.assert_not_called()  # Cancelled login is not a failure.
    assert app.login_window is replacement and replacement.is_active
    # A late destroy notification from a different window cannot drop this one.
    if old_login is not replacement:
        app._on_login_window_destroyed(old_login)
        assert app.login_window is replacement
    complete_login(app, glib)
    assert app.app_state.login_status == LoginStatus.LOGGED_IN


def test_close_hides_without_cancelling_login(session):
    app, glib = session
    login = app.login_window
    window, view = login._window, login._webview
    view.login()
    assert window.emit("close-request") is True
    assert not window.get_visible() and not window.destroyed
    app._show_login()
    assert app.login_window is login and window.get_visible()
    glib.advance(1000)
    view.reply_cookies()
    view.reply_params()
    assert app.login_window is None


def test_late_dom_check_and_data_clear_do_not_affect_reopened_window(session):
    app, glib = session
    login, view = app.login_window, app.login_window._webview
    view.emit("load-changed", "finished")
    glib.advance(2000)
    check = view.scripts.pop()
    app._do_logout()
    clearing_view = login._webview
    clear = clearing_view.clears.pop()
    login.destroy()
    app._show_login()
    new_view = app.login_window._webview
    app.app_state.login_status = LoginStatus.LOGGED_IN
    check(view, SimpleNamespace(to_boolean=lambda: True))
    clear(clearing_view, object())
    assert app.app_state.login_status == LoginStatus.LOGGED_IN
    assert len(new_view.loads) == 1


@pytest.fixture
def fallback(session, monkeypatch):
    app, glib = session
    for name in ("Overlay", "KeyboardWindow", "TrayIcon", "HotkeyManager"):
        monkeypatch.setattr(app_module, name, Mock())
    monkeypatch.setattr(app_module.X11KeyListener, "is_available", lambda: False)
    monkeypatch.setattr(app_module.EvdevListener, "is_available", lambda: False)
    monkeypatch.setattr(transcription, "AudioCapture", Mock())
    monkeypatch.setattr(transcription, "ASRClient", Mock())
    monkeypatch.setattr(transcription, "GLib", glib)
    app._setup_components()
    app.app_state.login_status = LoginStatus.LOGGED_IN
    return app, glib


@pytest.mark.parametrize("save_succeeds", [True, False])
def test_recording_fallback_saves_only_accepted_result_before_connecting(fallback, monkeypatch, save_succeeds):
    app, glib = fallback
    manager = app.transcription_manager
    save = Mock(wraps=ParamsStore.save) if save_succeeds else Mock(return_value=False)
    monkeypatch.setattr(ParamsStore, "save", save)
    manager.handle_start()
    manager.asr_client.connect.side_effect = lambda params: (
        pytest.fail("connected before saving") if ParamsStore.load() != params else None
    )
    manager.asr_client.connect.assert_not_called()
    view = app.login_window._webview
    view.reply_cookies()
    view.reply_params()
    save.assert_called_once()
    if save_succeeds:
        manager.asr_client.connect.assert_called_once_with(ParamsStore.load())
    else:
        manager.asr_client.connect.assert_not_called()
        assert app.app_state.session_status.fault == SessionFault.CREDENTIAL_SAVE
        glib.advance(int(SESSION_NOTICE_DURATION * 1000))
        manager.audio_capture.stop.assert_called_once()
        assert app.app_state.recording_state == RecordingState.IDLE
    assert app.login_window.is_active  # Fallback extraction does not close login.


@pytest.mark.parametrize("stage", ["cookies", "javascript"])
@pytest.mark.parametrize("action", ["logout", "destroy"])
def test_invalidated_recording_fallback_completes_and_stops_microphone(fallback, monkeypatch, stage, action):
    app, glib = fallback
    login, view = app.login_window, app.login_window._webview
    manager = app.transcription_manager
    save = Mock(wraps=ParamsStore.save)
    monkeypatch.setattr(ParamsStore, "save", save)
    manager.handle_start()
    manager.audio_capture.start.assert_called_once()
    assert app.app_state.recording_state == RecordingState.STARTING
    if stage == "javascript":
        view.reply_cookies()

    if action == "logout":
        app._do_logout()
    else:
        login.destroy()
    assert app.app_state.session_status.fault == SessionFault.PARAMETERS
    manager.audio_capture.stop.assert_called_once()
    assert app.app_state.recording_state == RecordingState.IDLE

    if stage == "cookies":
        view.reply_cookies()
        assert not view.scripts
    else:
        view.reply_params()
    login.destroy()
    glib.advance(5000)
    assert app.app_state.session_status.fault is None
    manager.audio_capture.stop.assert_called_once()
    manager.asr_client.connect.assert_not_called()
    save.assert_not_called()
    assert ParamsStore.load() is None


def test_retrieval_does_not_save_when_caller_rejects_result(session, monkeypatch):
    app, _ = session
    reject = Mock(return_value=None)
    save = Mock(wraps=ParamsStore.save)
    monkeypatch.setattr(ParamsStore, "save", save)
    app._extract_params(reject)
    view = app.login_window._webview
    view.reply_cookies()
    view.reply_params()
    reject.assert_called_once_with(ASRParams({"sessionid": "test-only"}, "123", "123"))
    save.assert_not_called()
    assert ParamsStore.load() is None


def test_each_outstanding_extraction_is_cancelled_once(session):
    app, _ = session
    callbacks = [Mock(), Mock()]
    view = app.login_window._webview
    for callback in callbacks:
        app._extract_params(callback)
    app._do_logout()
    view.reply_cookies()
    view.reply_cookies()
    app.login_window.destroy()
    for callback in callbacks:
        callback.assert_called_once_with(None)


def test_close_to_hide_preserves_recording_fallback(fallback):
    app, _ = fallback
    manager = app.transcription_manager
    login = app.login_window
    manager.handle_start()
    assert login._window.emit("close-request") is True
    assert app.app_state.error_message is None
    login._webview.reply_cookies()
    login._webview.reply_params()
    manager.asr_client.connect.assert_called_once_with(ParamsStore.load())
    manager.audio_capture.stop.assert_not_called()


def test_logout_without_existing_browser_clears_credentials(session):
    app, _ = session
    ParamsStore.save(ASRParams({"sessionid": "test-only"}, "d", "w"))
    app.login_window.destroy()
    app._do_logout()
    assert ParamsStore.load() is None
    assert app.app_state.login_status == LoginStatus.NOT_LOGGED_IN
    view = app.login_window._webview
    assert view.clears and not view.loads


def test_real_store_reports_write_failure(monkeypatch):
    from doubao_murmur import params_store

    path = Mock()
    path.write_text.side_effect = OSError("read-only")
    monkeypatch.setattr(params_store, "get_params_path", lambda: path)
    assert ParamsStore.save(ASRParams({}, "d", "w")) is False


@pytest.mark.parametrize("cached", [False, True])
def test_startup_and_recording_keep_first_and_return_login_paths(session, monkeypatch, cached):
    app, _ = session
    app.login_window.destroy()
    params = ASRParams({"sessionid": "test-only"}, "d", "w")
    if cached:
        ParamsStore.save(params)
    for name in ("Overlay", "KeyboardWindow", "TrayIcon", "HotkeyManager"):
        monkeypatch.setattr(app_module, name, Mock())
    monkeypatch.setattr(app_module.X11KeyListener, "is_available", lambda: False)
    monkeypatch.setattr(app_module.EvdevListener, "is_available", lambda: False)
    monkeypatch.setattr(transcription, "AudioCapture", Mock())
    monkeypatch.setattr(transcription, "ASRClient", Mock())
    app._setup_components()
    assert app.login_window is None
    manager = app.transcription_manager
    manager.handle_start()
    if cached:
        assert app.app_state.login_status == LoginStatus.LOGGED_IN
        manager.asr_client.connect.assert_called_once_with(params)
        assert app.login_window is None
    else:
        assert app.app_state.login_status == LoginStatus.NOT_LOGGED_IN
        transcription.ASRClient.return_value.connect.assert_not_called()
        manager.audio_capture.start.assert_not_called()
        assert app.login_window.is_active
