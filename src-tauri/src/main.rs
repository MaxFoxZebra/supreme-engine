// CV Studio: native shell around the bundled RenderCV server.
//
// Rendering a CV means running RenderCV, which is Python, so the app cannot be
// pure Rust without reimplementing its Typst templating. Rather than making the
// user install a Python toolchain, the Python side is frozen with PyInstaller
// and shipped as a bundled resource; this shell supervises it and points the OS
// webview at it. That keeps the Rust binary small, keeps the comment-preserving
// YAML round-trip (a Rust YAML crate would silently drop comments), and means
// the app works on a machine with nothing installed.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const SERVER_EXE: &str = if cfg!(windows) { "cv-studio-server.exe" } else { "cv-studio-server" };
const STARTUP_TIMEOUT: Duration = Duration::from_secs(300);
const SLOW_NOTICE_AFTER: Duration = Duration::from_secs(45);
const POLL_INTERVAL: Duration = Duration::from_millis(150);

/// Holds the child server process so it is cleanly terminated and reaped on exit.
struct ServerState(Mutex<Option<Child>>);

impl ServerState {
    fn new() -> Self {
        Self(Mutex::new(None))
    }

    fn set(&self, child: Child) {
        let mut guard = self.0.lock().unwrap_or_else(|e| e.into_inner());
        *guard = Some(child);
    }

    fn stop(&self) {
        let mut guard = self.0.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Ask the OS for an unused loopback port, then release it.
fn free_port() -> u16 {
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8722)
}

/// Find the frozen server: bundled resource first, then dev build output.
fn locate_server(app: &tauri::AppHandle) -> Option<PathBuf> {
    let mut candidates = Vec::with_capacity(4);
    if let Ok(dir) = app.path().resource_dir() {
        candidates.push(dir.join("server-dist").join(SERVER_EXE));
        candidates.push(dir.join(SERVER_EXE));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            candidates.push(d.join("server-dist").join(SERVER_EXE));
            candidates.push(d.join("../../../server/dist/cv-studio-server").join(SERVER_EXE));
        }
    }
    candidates.into_iter().find(|p| p.is_file())
}

fn port_open(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok()
}

/// The preferences file kept beside the app (matches `cjkfonts.app_data_dir`).
fn prefs_file() -> Option<PathBuf> {
    if let Some(override_dir) = std::env::var_os("CVSTUDIO_DATA_DIR") {
        return Some(PathBuf::from(override_dir).join("prefs.json"));
    }
    #[cfg(windows)]
    {
        std::env::var_os("LOCALAPPDATA")
            .map(|d| PathBuf::from(d).join("CV Studio").join("prefs.json"))
    }
    #[cfg(target_os = "macos")]
    {
        std::env::var_os("HOME").map(|h| {
            PathBuf::from(h).join("Library/Application Support/CV Studio/prefs.json")
        })
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let base = std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/share")))?;
        Some(base.join("cv-studio").join("prefs.json"))
    }
}

/// Whether closing the window should hide to tray (only when notifications are on).
fn keep_running() -> bool {
    let Some(v) = prefs_file()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
    else {
        return false;
    };
    let flag = |k: &str, default: bool| v.get(k).and_then(|x| x.as_bool()).unwrap_or(default);
    flag("notify", false) && flag("keep_running", true)
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

fn stop_server(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<ServerState>() {
        state.stop();
    }
}

fn js_quote(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_else(|_| "\"\"".to_string())
}

fn navigate_to(window: &WebviewWindow, url: &str) {
    let _ = window.eval(&format!("location.replace({})", js_quote(url)));
}

fn show_error(window: &WebviewWindow, msg: &str) {
    let _ = window.eval(&format!("window.studioError && window.studioError({})", js_quote(msg)));
}

fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    let open_i = MenuItem::with_id(app, "open", "Open CV Studio", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "Quit CV Studio", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open_i, &quit_i])?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("CV Studio")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main(app),
            "quit" => {
                stop_server(app);
                app.exit(0);
            }
            _ => {}
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    let _ = tray.build(app);
    Ok(())
}

fn launch_packaged_server(handle: tauri::AppHandle, window: WebviewWindow, port: u16) {
    let server = locate_server(&handle);
    std::thread::spawn(move || {
        let Some(server) = server else {
            show_error(
                &window,
                "The renderer is missing from this installation. Reinstall CV Studio.",
            );
            return;
        };

        let mut cmd = Command::new(&server);
        cmd.arg("--port")
            .arg(port.to_string())
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .arg("--app-version")
            .arg(env!("CARGO_PKG_VERSION"));

        if let Ok(t) = SystemTime::now().duration_since(UNIX_EPOCH) {
            cmd.arg("--spawned-at").arg(t.as_millis().to_string());
        }
        if let Some(dir) = server.parent() {
            cmd.current_dir(dir);
        }
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);

        match cmd.spawn() {
            Ok(child) => {
                if let Some(state) = handle.try_state::<ServerState>() {
                    state.set(child);
                }
            }
            Err(e) => {
                show_error(&window, &format!("Could not start the renderer: {e}"));
                return;
            }
        }

        let started = Instant::now();
        let mut told = false;
        while started.elapsed() < STARTUP_TIMEOUT {
            if port_open(port) {
                navigate_to(&window, &format!("http://127.0.0.1:{port}/"));
                return;
            }
            if !told && started.elapsed() > SLOW_NOTICE_AFTER {
                told = true;
                let _ = window.eval("window.studioSlow && window.studioSlow()");
            }
            std::thread::sleep(POLL_INTERVAL);
        }
        show_error(&window, "The renderer did not start within five minutes.");
    });
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_notification::init())
        .manage(ServerState::new())
        .setup(|app| {
            setup_tray(app)?;

            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("CV Studio")
                .inner_size(1480.0, 960.0)
                .min_inner_size(940.0, 620.0)
                .decorations(false)
                .focused(false)
                .build()?;

            // Development escape hatch: attach to an already-running dev server.
            //     python server/dev.py --no-open
            //     CVSTUDIO_DEV_URL=http://127.0.0.1:8722 cargo run
            if let Ok(dev_url) = std::env::var("CVSTUDIO_DEV_URL") {
                let w = window.clone();
                std::thread::spawn(move || {
                    let url = format!("{}/", dev_url.trim_end_matches('/'));
                    navigate_to(&w, &url);
                });
                return Ok(());
            }

            launch_packaged_server(app.handle().clone(), window, free_port());
            Ok(())
        })
        .on_window_event(|window, event| match event {
            tauri::WindowEvent::CloseRequested { api, .. } if keep_running() => {
                api.prevent_close();
                let _ = window.hide();
            }
            tauri::WindowEvent::Destroyed => stop_server(window.app_handle()),
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("error while building CV Studio");

    app.run(|app, event| match event {
        #[cfg(target_os = "macos")]
        tauri::RunEvent::Reopen { .. } => show_main(app),
        tauri::RunEvent::Exit => stop_server(app),
        _ => {}
    });
}
