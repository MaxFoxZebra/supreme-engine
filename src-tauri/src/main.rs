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

use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Holds the child so it can be killed on exit. Without this the server
/// outlives the window and keeps its port.
struct Server(Mutex<Option<Child>>);

const SERVER_EXE: &str = if cfg!(windows) { "cv-studio-server.exe" } else { "cv-studio-server" };

/// Ask the OS for an unused port, then release it. Slightly racy in theory, but
/// it avoids the far more common failure of a fixed port already being held by
/// a previous instance.
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8722)
}

/// Find the frozen server. Bundled location first, then the development build
/// output so a dev run tracks rebuilds without repackaging.
fn locate_server(app: &tauri::AppHandle) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(dir) = app.path().resource_dir() {
        candidates.push(dir.join("server-dist").join(SERVER_EXE));
        candidates.push(dir.join(SERVER_EXE));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            candidates.push(d.join("server-dist").join(SERVER_EXE));
            // cargo target/<profile>/ -> ../../server/dist/...
            candidates.push(
                d.join("../../../server/dist/cv-studio-server").join(SERVER_EXE),
            );
        }
    }
    candidates.into_iter().find(|p| p.is_file())
}

fn port_open(port: u16) -> bool {
    format!("127.0.0.1:{port}")
        .parse()
        .ok()
        .and_then(|addr| TcpStream::connect_timeout(&addr, Duration::from_millis(250)).ok())
        .is_some()
}

/// The preferences the server keeps for the interface, in the same app data
/// folder it uses (see prefs_path in studio.py and cache_dir in cjkfonts.py).
fn prefs_file() -> Option<PathBuf> {
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

/// Whether closing the window should leave the app running in the tray. Only
/// with notifications on, since reminders are the only thing it does unseen,
/// and not if the user turned that off. Read at the moment of closing, so a
/// change in Settings applies without telling the shell anything.
fn keep_running() -> bool {
    let Some(v) = prefs_file()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
    else {
        return false;
    };
    let on = |k: &str, default: bool| v.get(k).and_then(|x| x.as_bool()).unwrap_or(default);
    on("notify", false) && on("keep_running", true)
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

fn stop_server(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Server>() {
        if let Some(mut child) = state.0.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}

fn main() {
    let app = tauri::Builder::default()
        // First, so a second launch hands over to this one before it starts a
        // server of its own on the same workspace.
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
        .manage(Server(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let port = free_port();

            // The tray: where the app lives while the window is closed and
            // reminders are on. Built even when that is off, so there is
            // always one place to quit from; a failure to make one (no tray
            // on this desktop) is not a reason to stop.
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

            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("CV Studio")
                .inner_size(1480.0, 960.0)
                .min_inner_size(940.0, 620.0)
                // Frameless: the app draws its own title bar, so the window is
                // one continuous surface instead of an OS frame tinted with the
                // user's accent colour.
                .decorations(false)
                // Open without stealing focus. A utility that activates itself
                // will pull a fullscreen game or video back to the desktop.
                .focused(false)
                .build()?;

            // Development escape hatch: attach to an already-running dev server
            // instead of spawning a packaged one. That makes the shell itself
            // (frameless title bar, window controls, updater) testable against
            // live-reloading Python, with no repackaging in between.
            //     python server/dev.py --no-open
            //     CVSTUDIO_DEV_URL=http://127.0.0.1:8722 cargo run
            if let Ok(dev_url) = std::env::var("CVSTUDIO_DEV_URL") {
                let w = window.clone();
                std::thread::spawn(move || {
                    let url = dev_url.trim_end_matches('/').to_string();
                    let _ = w.eval(&format!("location.replace('{url}/')"));
                });
                return Ok(());
            }

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
                cmd.arg("--port").arg(port.to_string());
                // Let the renderer exit with us. Belt and braces alongside the
                // kill on window close, because a crashed parent never gets to
                // run that handler.
                cmd.arg("--parent-pid").arg(std::process::id().to_string());
                // Single source of truth for the version shown in About and the
                // API spec: whatever this build actually is.
                cmd.arg("--app-version").arg(env!("CARGO_PKG_VERSION"));
                if let Some(dir) = server.parent() {
                    cmd.current_dir(dir);
                }
                #[cfg(windows)]
                cmd.creation_flags(CREATE_NO_WINDOW);

                match cmd.spawn() {
                    Ok(child) => {
                        if let Some(state) = handle.try_state::<Server>() {
                            *state.0.lock().unwrap() = Some(child);
                        }
                    }
                    Err(e) => {
                        show_error(&window, &format!("Could not start the renderer: {e}"));
                        return;
                    }
                }

                let deadline = Instant::now() + Duration::from_secs(60);
                while Instant::now() < deadline {
                    if port_open(port) {
                        let _ = window.eval(&format!(
                            "location.replace('http://127.0.0.1:{port}/')"
                        ));
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(150));
                }
                show_error(&window, "The renderer did not start in time.");
            });

            Ok(())
        })
        .on_window_event(|window, event| match event {
            // With reminders on, closing hides the window rather than
            // quitting, or they would never come. Quit from the tray.
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
        // The Dock icon clicked while the window is hidden.
        #[cfg(target_os = "macos")]
        tauri::RunEvent::Reopen { .. } => show_main(app),
        tauri::RunEvent::Exit => stop_server(app),
        _ => {}
    });
}

fn show_error(window: &tauri::WebviewWindow, msg: &str) {
    let safe = msg.replace('\\', "\\\\").replace('\'', "\\'");
    let _ = window.eval(&format!(
        "window.studioError && window.studioError('{safe}')"
    ));
}
