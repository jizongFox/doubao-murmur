"""WebKitGTK-based login window for doubao.com.

Mirrors WebViewManager.swift.

- Loads doubao.com/chat in a WebKitGTK WebView
- Injects JS to detect login via /alice/profile/self API interception
- Extracts cookies + localStorage params after login
- Destroys WebView after params are extracted to free memory
"""

from __future__ import annotations

import importlib.resources
import json
import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from doubao_murmur.config import LOGIN_URL, WEBVIEW_USER_AGENT
from doubao_murmur.params_store import ASRParams

logger = logging.getLogger(__name__)

# Try WebKitGTK
_HAS_WEBKIT = False
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit

    _HAS_WEBKIT = True
except (ImportError, ValueError):
    # WebKit2 4.1 uses GTK3 and cannot be embedded in this GTK4 application.
    pass


def _as_id(value) -> str:
    """Normalise a localStorage id, which may be a number, to a string."""
    return "" if value is None else str(value).strip()


class LoginWindow:
    """WebKitGTK-based login window for doubao.com."""

    def __init__(
        self,
        *,
        on_params_ready: Callable[[ASRParams, str | None], bool] | None = None,
        on_login_failed: Callable[[], None] | None = None,
        on_login_required: Callable[[], None] | None = None,
        on_destroy: Callable[[LoginWindow], None] | None = None,
    ) -> None:
        # All callbacks run on the GTK thread. Only an accepted (persisted)
        # result closes the browser; failures leave it available for retry.
        self.on_params_ready = on_params_ready  # (params, nickname) -> bool
        self.on_login_failed = on_login_failed  # () -> None
        self.on_login_required = on_login_required  # () -> None
        self.on_destroy = on_destroy  # (window) -> None
        self._window: Gtk.Window | None = None
        self._webview = None
        self._generation = 0
        self._sources = set()
        self._pending_extractions = set()
        self._extracting_login = False
        self._clearing = False

    def _schedule(self, milliseconds, callback) -> None:
        generation = self._generation

        def run():
            self._sources.discard(source_id)
            if generation == self._generation:
                callback()
            return GLib.SOURCE_REMOVE

        source_id = GLib.timeout_add(milliseconds, run)
        self._sources.add(source_id)

    def _invalidate_pending(self) -> None:
        self._generation += 1
        for source_id in self._sources:
            GLib.source_remove(source_id)
        self._sources.clear()
        self._extracting_login = False
        pending, self._pending_extractions = self._pending_extractions, set()
        for complete in pending:
            complete(None)

    @property
    def is_active(self) -> bool:
        return self._webview is not None

    @staticmethod
    def is_available() -> bool:
        return _HAS_WEBKIT

    def _setup(self) -> None:
        if self._webview or not _HAS_WEBKIT:
            return

        # WebKitGTK configuration
        settings = WebKit.Settings()
        settings.set_property("enable-developer-extras", True)
        settings.set_property("user-agent", WEBVIEW_USER_AGENT)

        # User content manager for JS injection
        user_content = WebKit.UserContentManager()

        # Inject login detection JS at document start
        ws_js = self._load_js_resource("inject-websocket.js")
        if ws_js:
            # WebKitGTK uses the same messageHandlers API as WKWebView
            # Normalize handler name
            adapted_js = ws_js.replace(
                "window.webkit.messageHandlers.asrHandler.postMessage",
                "window.webkit.messageHandlers.asr_handler.postMessage"
            )
            script = WebKit.UserScript.new(
                adapted_js,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
                None,
                None,
            )
            user_content.add_script(script)

        # Inject DOM helpers at document end
        dom_js = self._load_js_resource("inject-dom.js")
        if dom_js:
            script = WebKit.UserScript.new(
                dom_js,
                WebKit.UserContentInjectedFrames.TOP_FRAME,
                WebKit.UserScriptInjectionTime.END,
                None,
                None,
            )
            user_content.add_script(script)

        # Register message handler. WebKitGTK 6 accepts a content-world
        # argument; older WebKit2GTK only accepts the handler name.
        try:
            user_content.register_script_message_handler("asr_handler", None)
        except TypeError:
            user_content.register_script_message_handler("asr_handler")
        generation = self._generation
        user_content.connect(
            "script-message-received::asr_handler",
            lambda manager, result: self._on_script_message(manager, result)
            if generation == self._generation and not self._clearing else None,
        )

        # Create WebView. WebKitGTK 6 has no
        # new_with_user_content_manager(); the construct-only property
        # works on both WebKitGTK 6 and WebKit2GTK 4.x.
        self._webview = WebKit.WebView(user_content_manager=user_content)
        self._webview.set_settings(settings)
        self._webview.connect("load-changed", self._on_load_changed)
        self._webview.connect("decide-policy", self._on_decide_policy)

        # Create window
        self._window = Gtk.Window()
        self._window.set_title("Doubao Murmur - 登录")
        self._window.set_default_size(1280, 800)
        self._window.set_child(self._webview)
        self._window.connect("close-request", self._on_close_request)

    def load(self) -> None:
        """Create webview (if needed) and load doubao.com."""
        self._setup()
        if self._webview and not self._clearing:
            self._webview.load_uri(LOGIN_URL)

    def show(self) -> None:
        if not self._webview:
            self.load()
        if self._window:
            self._window.present()

    def hide(self) -> None:
        if self._window:
            self._window.set_visible(False)

    def destroy(self) -> None:
        """Destroy WebView to free memory (mirrors destroyWebView())."""
        self._destroy_browser()
        if self.on_destroy:
            self.on_destroy(self)

    def _destroy_browser(self) -> None:
        webview, self._webview = self._webview, None
        window, self._window = self._window, None
        self._invalidate_pending()
        self._clearing = False
        if webview:
            webview.stop_loading()
        if window:
            window.destroy()
        logger.info("WebView destroyed")

    def extract_params_async(self, callback: Callable[[ASRParams | None], None]) -> None:
        """Extract cookies + localStorage params from WebView.

        Complete exactly once on the GTK main thread. Failure or browser
        invalidation (logout/destroy) returns None; hiding preserves the request.
        """
        if not self._webview or self._clearing:
            callback(None)
            return

        webview = self._webview
        generation = self._generation
        delivered = False

        def current():
            return generation == self._generation and webview is self._webview

        def deliver(params):
            nonlocal delivered
            if not delivered:
                delivered = True
                self._pending_extractions.discard(deliver)
                callback(params if current() else None)

        self._pending_extractions.add(deliver)

        # Step 1: Get cookies via WebKit.CookieManager
        cookie_manager = self._get_cookie_manager()

        def on_cookies_finish(source, result=None, *_args):
            if not current() or delivered:
                return
            if result is None:
                result = source
            try:
                cookies = cookie_manager.get_cookies_finish(result)
            except Exception:
                deliver(None)
                return

            doubao_cookies: dict[str, str] = {}
            for cookie in cookies:
                domain = cookie.get_domain()
                if "doubao.com" in domain:
                    doubao_cookies[cookie.get_name()] = cookie.get_value()

            if not doubao_cookies:
                logger.warning("No doubao.com cookies found")
                deliver(None)
                return

            # Step 2: Extract localStorage values via JS
            self._extract_local_storage(doubao_cookies, deliver, current)

        try:
            cookie_manager.get_cookies(
                LOGIN_URL,
                None,  # cancellable
                on_cookies_finish,
                None,  # user_data
            )
        except Exception as e:
            logger.error("Cookie extraction failed: %s", e)
            deliver(None)

    def _extract_local_storage(self, cookies: dict, callback, current) -> None:
        """Extract device_id and web_id from localStorage."""
        js_code = """
        JSON.stringify({
            device_id_raw: localStorage.getItem('samantha_web_web_id'),
            tea_cache_raw: localStorage.getItem('__tea_cache_tokens_497858')
        })
        """

        webview = self._webview

        def on_js_finish(source, result=None, *_args):
            if not current():
                return
            if result is None:
                result = source
            try:
                js_value = webview.evaluate_javascript_finish(result)
                json_str = js_value.to_string()
                data = json.loads(json_str)

                device_id = ""
                web_id = ""
                tea_cache = {}

                if data.get("device_id_raw"):
                    parsed = json.loads(data["device_id_raw"])
                    device_id = _as_id(parsed.get("web_id"))

                if data.get("tea_cache_raw"):
                    tea_cache = json.loads(data["tea_cache_raw"])
                    web_id = _as_id(tea_cache.get("web_id"))

                # doubao.com dropped the `samantha_web_web_id` entry when it
                # renamed its localStorage keys to the `flow_*` scheme, so
                # device_id came back empty and no params were ever saved.
                # The tea SDK cache still carries the same id.
                if not device_id:
                    device_id = _as_id(
                        tea_cache.get("user_unique_id")
                    ) or web_id

                if device_id and web_id:
                    params = ASRParams(
                        cookies=cookies,
                        device_id=device_id,
                        web_id=web_id,
                    )
                    logger.info(
                        "Params extracted: %d cookies, device=%s, web=%s",
                        len(cookies),
                        device_id[:10],
                        web_id[:10],
                    )
                    callback(params)
                else:
                    logger.warning(
                        "Missing localStorage params: device=%s, web=%s",
                        device_id,
                        web_id,
                    )
                    callback(None)
            except Exception as e:
                logger.error("JS evaluation failed: %s", e)
                callback(None)

        try:
            webview.evaluate_javascript(
                js_code,
                -1,
                None,  # world_name
                None,  # source_uri
                None,  # cancellable
                on_js_finish,
                None,  # user_data
            )
        except Exception as e:
            logger.error("JS evaluation failed: %s", e)
            callback(None)

    def _get_cookie_manager(self):
        """WebKitGTK 6 moved cookies to NetworkSession; 4.x uses the
        website data manager."""
        if hasattr(self._webview, "get_network_session"):
            return self._webview.get_network_session().get_cookie_manager()
        return self._webview.get_website_data_manager().get_cookie_manager()

    def _get_website_data_manager(self):
        if hasattr(self._webview, "get_network_session"):
            return self._webview.get_network_session().get_website_data_manager()
        return self._webview.get_website_data_manager()

    def _on_script_message(self, manager, js_result) -> None:
        """Handle messages from injected JS (login detection)."""
        try:
            json_str = self._json_string_from_js_value(js_result)
            data = json.loads(json_str)
        except Exception:
            return

        msg_type = data.get("type")
        if msg_type == "login":
            status = data.get("status", "unknown")
            nickname = data.get("nickname")
            self._notify_login_status(status, nickname)

    def _on_load_changed(self, webview, event) -> None:
        if (webview is self._webview and not self._clearing
                and event == WebKit.LoadEvent.FINISHED):
            self._schedule(2000, self._check_login_fallback)

    def _on_decide_policy(self, webview, decision, decision_type) -> bool:
        """Detect login redirect via URL parameter."""
        if (webview is self._webview and not self._clearing
                and decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION):
            nav_action = decision.get_navigation_action()
            uri = nav_action.get_request().get_uri()
            if "from_login=1" in uri:
                self._notify_login_status("loggedIn", None)
        return False  # Allow default handling

    def _check_login_fallback(self) -> bool:
        """Fallback: check if login button is present in DOM."""
        if not self._webview:
            return GLib.SOURCE_REMOVE
        js = "window.__doubaoMurmur && window.__doubaoMurmur.isLoginButtonPresent()"
        generation = self._generation
        self._webview.evaluate_javascript(
            js,
            -1,
            None,
            None,
            None,
            lambda *args: self._on_login_check_result(*args)
            if generation == self._generation else None,
            None,
        )
        return GLib.SOURCE_REMOVE

    def _on_login_check_result(self, source, result=None, *_args) -> None:
        if result is None:
            result = source
        try:
            if self._webview:
                val = self._webview.evaluate_javascript_finish(result)
                if val.to_boolean():
                    self._notify_login_status("loggedOut", None)
        except Exception:
            pass

    def _on_close_request(self, window) -> bool:
        """Hide instead of destroy when user closes the window."""
        window.set_visible(False)
        return True  # Prevent default destroy

    def logout(self) -> None:
        """Invalidate browser work, clear website data, then reload login."""
        # Recreate the browser so old document signals cannot authenticate the
        # new session. Keep this LoginWindow (and its public callbacks) alive.
        visible = self._window is not None and self._window.get_visible()
        self._destroy_browser()
        self._setup()
        self._clearing = True
        generation = self._generation

        def reload():
            if generation != self._generation:
                return
            self._clearing = False
            self.load()
            if visible:
                self.show()

        self._clear_website_data(reload)

    def _clear_website_data(self, callback=None) -> None:
        """Clear WebKitGTK website data for this app before reloading login."""
        if not self._webview:
            if callback:
                callback()
            return

        data_manager = self._get_website_data_manager()
        clear = getattr(data_manager, "clear", None)
        if clear is None:
            if callback:
                callback()
            return

        types = getattr(WebKit.WebsiteDataTypes, "ALL", None)
        if types is None:
            types = 0xFFFFFFFF

        def on_clear_finished(source, result=None, *_args):
            finish = getattr(data_manager, "clear_finish", None)
            if finish is not None and result is not None:
                try:
                    finish(result)
                except Exception as e:
                    logger.warning("Failed to finish WebKit data clear: %s", e)
            if callback:
                callback()

        try:
            clear(types, 0, None, on_clear_finished, None)
        except TypeError:
            clear(types, 0, None, on_clear_finished)

    def _notify_login_status(self, status: str, nickname: str | None) -> None:
        if not self._webview or self._clearing or self._extracting_login:
            return
        if status != "loggedIn":
            if self.on_login_required:
                self.on_login_required()
            return
        self._extracting_login = True
        generation = self._generation

        def on_params(params):
            if generation != self._generation:
                return
            if params is None:
                if self.on_login_failed:
                    self.on_login_failed()
                accepted = False
            else:
                accepted = self.on_params_ready and self.on_params_ready(params, nickname)
            if generation != self._generation:
                return
            if accepted:
                self.destroy()
            else:
                self._extracting_login = False

        # Cookies may not yet be committed when the profile/redirect arrives.
        self._schedule(1000, lambda: self.extract_params_async(on_params))

    @staticmethod
    def _json_string_from_js_value(value) -> str:
        """Convert WebKitGTK/JSC script message values to a JSON string."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return json.dumps(value)
        if hasattr(value, "get_js_value"):
            value = value.get_js_value()
        to_json = getattr(value, "to_json", None)
        if to_json is not None:
            try:
                result = to_json(0)
            except TypeError:
                result = to_json()
            if result:
                return result
        to_string = getattr(value, "to_string", None)
        if to_string is not None:
            return to_string()
        raise TypeError(f"Unsupported JS value: {type(value)!r}")

    @staticmethod
    def _load_js_resource(name: str) -> str | None:
        """Load JS file from resources directory."""
        try:
            pkg = importlib.resources.files("doubao_murmur.resources")
            return (pkg / name).read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Cannot load JS resource %s: %s", name, e)
            return None
