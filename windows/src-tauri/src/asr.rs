//! WebSocket client for Doubao's streaming ASR service. Mirrors DoubaoASRClient.swift.
//!
//! The audio channel is unbounded and created before the socket, so chunks
//! recorded while the handshake is still in flight queue up and are drained the
//! moment the connection opens — the same "buffer until connected" behaviour the
//! macOS and Linux clients implement by hand.

use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::{HeaderValue, StatusCode};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::{Error, Message};

use crate::config;
use crate::params::AsrParams;
use crate::{log_error, log_info, log_warn};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AsrEvent {
    Opened,
    Result(String),
    Finished,
    /// Network/protocol failure; must not invalidate saved credentials.
    Error(String),
    /// Login rejected by the service; the controller owns clearing and prompting.
    AuthError,
}

enum Ctl {
    Finish,
    Disconnect,
}

pub struct AsrClient {
    ctl_tx: Option<UnboundedSender<Ctl>>,
}

impl AsrClient {
    pub fn new() -> AsrClient {
        AsrClient { ctl_tx: None }
    }

    /// `audio_rx` is created by the caller before recording starts, so chunks
    /// captured while the handshake is in flight are already queued when the
    /// socket opens.
    pub fn connect<F>(
        &mut self,
        params: AsrParams,
        audio_rx: UnboundedReceiver<Vec<u8>>,
        emit: F,
    ) where
        F: Fn(AsrEvent) + Send + 'static,
    {
        self.disconnect();

        let (ctl_tx, ctl_rx) = unbounded_channel::<Ctl>();
        self.ctl_tx = Some(ctl_tx);

        std::thread::spawn(move || {
            let runtime = match tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            {
                Ok(runtime) => runtime,
                Err(e) => {
                    emit(AsrEvent::Error(format!("无法创建运行时: {e}")));
                    return;
                }
            };
            runtime.block_on(run(params, audio_rx, ctl_rx, emit));
        });
    }

    /// Stop sending audio but keep the socket open for the final result.
    pub fn finish_sending(&self) {
        if let Some(tx) = &self.ctl_tx {
            let _ = tx.send(Ctl::Finish);
        }
        log_info!("Finished sending audio, waiting for server response");
    }

    pub fn disconnect(&mut self) {
        if let Some(tx) = self.ctl_tx.take() {
            let _ = tx.send(Ctl::Disconnect);
        }
    }
}

impl Drop for AsrClient {
    fn drop(&mut self) {
        self.disconnect();
    }
}

/// Builds the full WSS URL. Public for tests; `web_tab_id` is passed in so the
/// result is deterministic.
pub fn build_url(params: &AsrParams, web_tab_id: &str) -> String {
    let mut query: Vec<(String, String)> = config::FIXED_QUERY_PARAMS
        .iter()
        .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
        .collect();

    query.push(("device_id".into(), params.device_id.clone()));
    query.push(("web_id".into(), params.web_id.clone()));
    // tea_uuid intentionally mirrors web_id, matching the macOS client.
    query.push(("tea_uuid".into(), params.web_id.clone()));
    query.push(("web_tab_id".into(), web_tab_id.to_string()));

    let encoded = query
        .iter()
        .map(|(key, value)| format!("{}={}", percent_encode(key), percent_encode(value)))
        .collect::<Vec<_>>()
        .join("&");

    format!("{}?{}", config::WSS_BASE_URL, encoded)
}

/// Minimal application/x-www-form-urlencoded component encoder.
fn percent_encode(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

async fn run<F: Fn(AsrEvent)>(
    params: AsrParams,
    mut audio_rx: UnboundedReceiver<Vec<u8>>,
    mut ctl_rx: UnboundedReceiver<Ctl>,
    emit: F,
) {
    let url = build_url(&params, &uuid::Uuid::new_v4().to_string());

    let request = match make_request(&url, &params) {
        Ok(request) => request,
        Err(e) => {
            log_error!("Could not build the WebSocket request: {e}");
            emit(AsrEvent::Error(e));
            return;
        }
    };

    log_info!("Connecting to ASR WebSocket");
    let socket = match tokio::time::timeout(
        config::CONNECT_TIMEOUT,
        tokio_tungstenite::connect_async(request),
    )
    .await
    {
        Ok(Ok((socket, _response))) => socket,
        Ok(Err(e)) => {
            log_error!("WebSocket connect failed: {e}");
            emit(handshake_failure(e));
            return;
        }
        Err(_) => {
            log_error!("WebSocket connect timed out");
            emit(AsrEvent::Error("连接超时".to_string()));
            return;
        }
    };

    log_info!("Connected");
    let (mut write, mut read) = socket.split();
    emit(AsrEvent::Opened);

    let mut sending = true;
    let mut report_close_error = true;
    let mut received_any = false;

    loop {
        tokio::select! {
            biased;

            ctl = ctl_rx.recv() => match ctl {
                Some(Ctl::Finish) => {
                    // Flush trailing silence so the server finalises the last
                    // word before we stop feeding it audio; see
                    // config::STOP_TRAILING_SILENCE_MS.
                    let silence = vec![0u8; config::audio_chunk_samples() * 2];
                    for _ in 0..config::stop_trailing_silence_chunks() {
                        if let Err(e) = write.send(Message::Binary(silence.clone())).await {
                            log_warn!("Silence padding send failed: {e}");
                            break;
                        }
                    }
                    sending = false;
                }
                Some(Ctl::Disconnect) | None => {
                    report_close_error = false;
                    break;
                }
            },

            chunk = audio_rx.recv(), if sending => match chunk {
                Some(chunk) => {
                    if let Err(e) = write.send(Message::Binary(chunk)).await {
                        log_warn!("Audio send failed: {e}");
                        break;
                    }
                }
                None => sending = false,
            },

            incoming = read.next() => match incoming {
                Some(Ok(Message::Text(text))) => {
                    received_any = true;
                    if handle_message(&text, &emit) {
                        report_close_error = false;
                        break;
                    }
                }
                Some(Ok(Message::Binary(bytes))) => {
                    received_any = true;
                    let text = String::from_utf8_lossy(&bytes).into_owned();
                    if handle_message(&text, &emit) {
                        report_close_error = false;
                        break;
                    }
                }
                Some(Ok(Message::Close(frame))) => {
                    log_info!("Server closed the socket: {frame:?}");
                    emit(server_close_event(received_any, frame.map(|frame| frame.code)));
                    report_close_error = false; // A terminal event was already reported.
                    break;
                }
                Some(Ok(_)) => {}
                Some(Err(e)) => {
                    log_error!("WebSocket receive failed: {e}");
                    emit(AsrEvent::Error(e.to_string()));
                    return;
                }
                None => break,
            },
        }
    }

    if report_close_error {
        emit(AsrEvent::Error("连接已关闭".to_string()));
    }

    let _ = write.close().await;
    log_info!("Disconnected");
}

fn make_request(
    url: &str,
    params: &AsrParams,
) -> Result<tokio_tungstenite::tungstenite::handshake::client::Request, String> {
    let mut request = url
        .into_client_request()
        .map_err(|e| format!("URL 无效: {e}"))?;

    let headers = request.headers_mut();
    headers.insert(
        "Cookie",
        HeaderValue::from_str(&params.cookie_header()).map_err(|e| format!("Cookie 无效: {e}"))?,
    );
    headers.insert("Origin", HeaderValue::from_static(config::ORIGIN));
    headers.insert(
        "User-Agent",
        HeaderValue::from_static(config::WEBVIEW_USER_AGENT),
    );

    Ok(request)
}

/// Returns true when the caller should stop reading (auth failure).
fn handle_message(text: &str, emit: &dyn Fn(AsrEvent)) -> bool {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
        return false;
    };

    let code = value.get("code").and_then(|c| c.as_i64()).unwrap_or(0);
    let message = value.get("message").and_then(|m| m.as_str()).unwrap_or("");

    if code != 0 && is_auth_error(code, message) {
        log_warn!("Auth error detected: code={code}, message={message}");
        emit(AsrEvent::AuthError);
        return true;
    }

    match value.get("event").and_then(|e| e.as_str()).unwrap_or("") {
        "result" => {
            let text = value
                .get("result")
                .and_then(|r| r.get("Text"))
                .and_then(|t| t.as_str())
                .unwrap_or("");
            if !text.is_empty() {
                emit(AsrEvent::Result(text.to_string()));
            }
        }
        "finish" => {
            log_info!("Received finish event");
            emit(AsrEvent::Finished);
        }
        _ => {}
    }

    false
}

pub fn is_auth_error(code: i64, message: &str) -> bool {
    if config::AUTH_ERROR_CODES.contains(&code) {
        return true;
    }
    let lower = message.to_lowercase();
    config::AUTH_ERROR_KEYWORDS
        .iter()
        .any(|keyword| lower.contains(keyword))
}

fn handshake_failure(error: Error) -> AsrEvent {
    // Classify protocol evidence here, not in the controller based on where
    // credentials came from. A timeout, DNS failure, or gateway error says
    // nothing about whether a saved login is still valid.
    let rejected = match &error {
        Error::Http(response) => match response.status() {
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => true,
            StatusCode::OK => response
                .body()
                .as_deref()
                .and_then(|body| serde_json::from_slice::<serde_json::Value>(body).ok())
                .map(|value| {
                    let code = value.get("code").and_then(|c| c.as_i64()).unwrap_or(0);
                    let message = value.get("message").and_then(|m| m.as_str()).unwrap_or("");
                    code != 0 && is_auth_error(code, message)
                })
                .unwrap_or(false),
            _ => false,
        },
        _ => false,
    };
    if rejected {
        AsrEvent::AuthError
    } else {
        AsrEvent::Error(error.to_string())
    }
}

fn server_close_event(received_any: bool, code: Option<CloseCode>) -> AsrEvent {
    // The service accepts an expired login's handshake, then closes normally
    // before sending any data. A valid stream may also end with 1000 after
    // results. Only the first case is authentication rejection (as on Linux).
    // Our own disconnect is handled by Ctl::Disconnect, never this classifier.
    if !received_any && code == Some(CloseCode::Normal) {
        AsrEvent::AuthError
    } else {
        AsrEvent::Error("连接已关闭".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn sample_params() -> AsrParams {
        let mut params = AsrParams::default();
        params.cookies.insert("sessionid".into(), "abc".into());
        params.device_id = "1234567890".into();
        params.web_id = "9876543210".into();
        params
    }

    fn query_of(url: &str) -> HashMap<String, String> {
        url.split_once('?')
            .expect("url has a query")
            .1
            .split('&')
            .filter(|pair| !pair.is_empty())
            .map(|pair| {
                let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
                (key.to_string(), value.to_string())
            })
            .collect()
    }

    #[test]
    fn url_targets_the_doubao_endpoint() {
        let url = build_url(&sample_params(), "tab-1");
        assert!(url.starts_with(&format!("{}?", config::WSS_BASE_URL)));
    }

    #[test]
    fn url_carries_every_fixed_parameter() {
        let query = query_of(&build_url(&sample_params(), "tab-1"));
        for (key, value) in config::FIXED_QUERY_PARAMS {
            assert_eq!(query.get(*key).map(String::as_str), Some(*value), "{key}");
        }
    }

    #[test]
    fn url_wires_identifiers_from_params() {
        let params = sample_params();
        let query = query_of(&build_url(&params, "tab-1"));
        assert_eq!(query.get("device_id"), Some(&params.device_id));
        assert_eq!(query.get("web_id"), Some(&params.web_id));
        assert_eq!(query.get("tea_uuid"), Some(&params.web_id));
        assert_eq!(query.get("web_tab_id").map(String::as_str), Some("tab-1"));
    }

    #[test]
    fn auth_errors_are_recognised() {
        for code in config::AUTH_ERROR_CODES {
            assert!(is_auth_error(*code, ""));
        }
        assert!(is_auth_error(1, "Cookie is invalid"));
        assert!(is_auth_error(2, "AUTH failed"));
        assert!(is_auth_error(3, "session expired"));
    }

    #[test]
    fn unrelated_errors_are_not_auth_errors() {
        assert!(!is_auth_error(500, "internal server error"));
        assert!(!is_auth_error(42, "rate limited"));
    }

    fn http_failure(status: u16, body: Option<&[u8]>) -> AsrEvent {
        let response = tokio_tungstenite::tungstenite::http::Response::builder()
            .status(status)
            .body(body.map(|body| body.to_vec()))
            .unwrap();
        handshake_failure(Error::Http(response))
    }

    #[test]
    fn handshake_authentication_rejections_have_a_distinct_event() {
        for status in [401, 403] {
            assert_eq!(http_failure(status, None), AsrEvent::AuthError);
        }
        assert_eq!(
            http_failure(200, Some(br#"{"code":710012001,"message":"login invalid"}"#)),
            AsrEvent::AuthError
        );
        assert_eq!(
            http_failure(200, Some(br#"{"code":710012001}"#)),
            AsrEvent::AuthError
        );
    }

    #[test]
    fn unrelated_or_incomplete_handshake_bodies_preserve_credentials() {
        for body in [
            None,
            Some(b"".as_slice()),
            Some(b"not json".as_slice()),
            Some(br#"{"code":0,"message":"session"}"#.as_slice()),
            Some(br#"{"code":42,"message":"rate limited"}"#.as_slice()),
            Some(br#"["login invalid"]"#.as_slice()),
        ] {
            assert!(matches!(http_failure(200, body), AsrEvent::Error(_)));
        }
    }

    #[test]
    fn gateway_and_rate_limit_responses_are_not_login_rejections() {
        for status in [429, 500, 502, 503] {
            assert!(matches!(
                http_failure(
                    status,
                    Some(br#"{"code":710012001,"message":"session backend unavailable"}"#)
                ),
                AsrEvent::Error(_)
            ));
        }
    }

    #[test]
    fn timeouts_dns_and_connection_resets_do_not_expire_login() {
        for kind in [
            std::io::ErrorKind::TimedOut,
            std::io::ErrorKind::NotFound,
            std::io::ErrorKind::ConnectionReset,
        ] {
            let error = Error::Io(std::io::Error::new(kind, "network unavailable"));
            assert!(matches!(handshake_failure(error), AsrEvent::Error(_)));
        }
    }

    #[test]
    fn only_a_normal_server_close_before_data_indicates_expired_login() {
        assert_eq!(
            server_close_event(false, Some(CloseCode::Normal)),
            AsrEvent::AuthError
        );
        assert!(matches!(
            server_close_event(true, Some(CloseCode::Normal)),
            AsrEvent::Error(_)
        ));
        for code in [None, Some(CloseCode::Away), Some(CloseCode::Error)] {
            assert!(matches!(server_close_event(false, code), AsrEvent::Error(_)));
            assert!(matches!(server_close_event(true, code), AsrEvent::Error(_)));
        }
    }

    #[test]
    fn tourist_limit_and_login_invalid_stop_listening_after_one_auth_event() {
        for code in config::AUTH_ERROR_CODES {
            let seen = std::sync::Mutex::new(Vec::new());
            let emit = |event: AsrEvent| seen.lock().unwrap().push(event);
            let text = format!(r#"{{"code":{code},"result":null}}"#);
            assert!(handle_message(&text, &emit));
            assert_eq!(*seen.lock().unwrap(), vec![AsrEvent::AuthError]);
        }
    }

    #[test]
    fn result_events_yield_text() {
        let seen = std::sync::Mutex::new(Vec::new());
        let emit = |event: AsrEvent| seen.lock().unwrap().push(format!("{event:?}"));

        assert!(!handle_message(
            r#"{"code":0,"event":"result","result":{"Text":"你好"}}"#,
            &emit
        ));
        assert!(seen.lock().unwrap()[0].contains("你好"));
    }

    #[test]
    fn empty_results_are_ignored() {
        let seen = std::sync::Mutex::new(0usize);
        let emit = |_: AsrEvent| *seen.lock().unwrap() += 1;

        assert!(!handle_message(
            r#"{"code":0,"event":"result","result":{"Text":""}}"#,
            &emit
        ));
        assert_eq!(*seen.lock().unwrap(), 0);
    }

    #[test]
    fn auth_failures_stop_the_read_loop() {
        let emit = |_: AsrEvent| {};
        assert!(handle_message(
            r#"{"code":709599054,"message":"invalid"}"#,
            &emit
        ));
    }

    #[test]
    fn malformed_json_is_ignored() {
        let emit = |_: AsrEvent| {};
        assert!(!handle_message("not json", &emit));
    }

    #[test]
    fn percent_encoding_escapes_reserved_characters() {
        assert_eq!(percent_encode("a b"), "a%20b");
        assert_eq!(percent_encode("use-olympus_account.x~"), "use-olympus_account.x~");
    }
}
