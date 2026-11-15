//! Agent Hub for Windows: the server's web UI in a native window, with native notifications
//! and a tray icon so notifications keep arriving when the window is closed.
//!
//! Security model:
//! - The server address is chosen at first run (nothing about your server is built in).
//! - The server's pages get exactly two native abilities, granted at runtime for that one
//!   origin: show notifications, and open http(s) links in the default browser. Nothing else
//!   (no file system, no shell, no window control).
//! - The window only ever shows the server or the bundled connect screen; any other address
//!   is handed to the default browser instead.

use std::{
    fs,
    net::{TcpStream, ToSocketAddrs},
    path::PathBuf,
    sync::Mutex,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use tauri::{
    ipc::CapabilityBuilder,
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_opener::OpenerExt;
use url::Url;

const MAIN: &str = "main";

#[derive(Default, Serialize, Deserialize)]
struct Settings {
    server: Option<String>,
}

/// The origin the window is allowed to show (scheme://host:port), once connected.
struct ServerOrigin(Mutex<Option<String>>);

fn settings_path(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_config_dir().ok().map(|d| d.join("settings.json"))
}

fn load_settings(app: &AppHandle) -> Settings {
    settings_path(app)
        .and_then(|p| fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_settings(app: &AppHandle, settings: &Settings) -> Result<(), String> {
    let path = settings_path(app).ok_or("no settings directory")?;
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    fs::write(&path, serde_json::to_string_pretty(settings).unwrap()).map_err(|e| e.to_string())
}

fn origin_of(url: &Url) -> String {
    url.origin().ascii_serialization()
}

/// Parse and check what the user typed: an http(s) URL with a host, reduced to its origin.
fn parse_server(input: &str) -> Result<Url, String> {
    let with_scheme = if input.contains("://") { input.to_string() } else { format!("https://{input}") };
    let url = Url::parse(&with_scheme).map_err(|_| "That isn't a valid address.".to_string())?;
    if !matches!(url.scheme(), "https" | "http") || url.host_str().is_none() {
        return Err("Use an http:// or https:// address.".into());
    }
    Url::parse(&origin_of(&url)).map_err(|e| e.to_string())
}

/// Quick reachability check, so a typo or a disconnected Tailscale gives a clear message.
fn reachable(url: &Url) -> Result<(), String> {
    let host = url.host_str().unwrap_or_default();
    let port = url.port_or_known_default().unwrap_or(443);
    let addrs = (host, port)
        .to_socket_addrs()
        .map_err(|_| format!("Can't find {host}. Is Tailscale connected on this PC?"))?;
    for addr in addrs {
        if TcpStream::connect_timeout(&addr, Duration::from_secs(4)).is_ok() {
            return Ok(());
        }
    }
    Err(format!("{host}:{port} isn't answering. Is Tailscale connected, and is the port allowed in your ACL?"))
}

/// Grant the server's pages their two native abilities (for that origin only).
fn grant_server(app: &AppHandle, origin: &str) -> Result<(), String> {
    let id: String = origin.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
    let capability = CapabilityBuilder::new(format!("server-{id}"))
        .remote(format!("{origin}/*"))
        .window(MAIN)
        .permission("notification:allow-is-permission-granted")
        .permission("notification:allow-request-permission")
        .permission("notification:allow-notify")
        .permission("opener:allow-open-url")
        .permission("opener:allow-default-urls");
    app.add_capability(capability).map_err(|e| e.to_string())
}

fn open_server(app: &AppHandle, url: &Url) -> Result<(), String> {
    let origin = origin_of(url);
    grant_server(app, &origin)?;
    *app.state::<ServerOrigin>().0.lock().unwrap() = Some(origin);
    if let Some(win) = app.get_webview_window(MAIN) {
        win.navigate(url.clone()).map_err(|e| e.to_string())?;
        show(&win);
    }
    Ok(())
}

#[tauri::command]
fn current_server(app: AppHandle) -> Option<String> {
    load_settings(&app).server
}

#[tauri::command]
async fn connect(app: AppHandle, url: String) -> Result<(), String> {
    let url = parse_server(url.trim())?;
    let check = url.clone();
    tauri::async_runtime::spawn_blocking(move || reachable(&check))
        .await
        .map_err(|e| e.to_string())??;
    save_settings(&app, &Settings { server: Some(url.to_string()) })?;
    open_server(&app, &url)
}

fn show(win: &WebviewWindow) {
    let _ = win.unminimize();
    let _ = win.show();
    let _ = win.set_focus();
}

fn change_server(app: &AppHandle) {
    let mut settings = load_settings(app);
    settings.server = None;
    let _ = save_settings(app, &settings);
    app.restart();
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open Agent Hub", true, None::<&str>)?;
    let change = MenuItem::with_id(app, "change", "Change server…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&open, &change, &sep, &quit])?;
    TrayIconBuilder::with_id("tray")
        .icon(app.default_window_icon().cloned().expect("app icon"))
        .tooltip("Agent Hub")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => {
                if let Some(w) = app.get_webview_window(MAIN) {
                    show(&w);
                }
            }
            "change" => change_server(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                if let Some(w) = tray.app_handle().get_webview_window(MAIN) {
                    show(&w);
                }
            }
        })
        .build(app)?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // A second launch just brings the existing window forward.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window(MAIN) {
                show(&w);
            }
        }))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .manage(ServerOrigin(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![connect, current_server])
        .setup(|app| {
            let handle = app.handle().clone();
            let saved = load_settings(&handle).server.and_then(|s| parse_server(&s).ok());

            let nav_handle = handle.clone();
            let win = WebviewWindowBuilder::new(app, MAIN, WebviewUrl::App("index.html".into()))
                .title("Agent Hub")
                .inner_size(1280.0, 860.0)
                .min_inner_size(420.0, 560.0)
                .on_navigation(move |target| {
                    // The bundled connect screen, or the chosen server: fine. Anything else goes
                    // to the default browser (previews, GitHub, docs links).
                    let local = matches!(target.scheme(), "tauri" | "about")
                        || target.host_str() == Some("tauri.localhost");
                    let server = nav_handle.state::<ServerOrigin>().0.lock().unwrap().clone();
                    if local || server.as_deref() == Some(origin_of(target).as_str()) {
                        return true;
                    }
                    if matches!(target.scheme(), "http" | "https") {
                        let _ = nav_handle.opener().open_url(target.as_str(), None::<&str>);
                    }
                    false
                })
                .build()?;

            // Closing the window keeps the app in the tray, so notifications keep coming.
            let hide_win = win.clone();
            win.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = hide_win.hide();
                }
            });

            build_tray(&handle)?;

            if let Some(url) = saved {
                if let Err(e) = open_server(&handle, &url) {
                    eprintln!("couldn't open {url}: {e}");
                }
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Agent Hub");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_addresses_are_reduced_to_their_origin() {
        assert_eq!(parse_server("box.tailnet.ts.net").unwrap().as_str(), "https://box.tailnet.ts.net/");
        assert_eq!(parse_server("https://box.ts.net/sandboxes/abc?x=1").unwrap().as_str(), "https://box.ts.net/");
        assert_eq!(parse_server("http://100.64.0.1:8090").unwrap().as_str(), "http://100.64.0.1:8090/");
        assert!(parse_server("file:///etc/passwd").is_err());
        assert!(parse_server("javascript:alert(1)").is_err());
        assert!(parse_server("not a url").is_err());
    }
}
