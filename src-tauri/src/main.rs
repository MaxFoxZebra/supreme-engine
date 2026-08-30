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

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .manage(Server(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let port = free_port();

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
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.app_handle().try_state::<Server>() {
                    if let Some(mut child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running CV Studio");
}

fn show_error(window: &tauri::WebviewWindow, msg: &str) {
    let safe = msg.replace('\\', "\\\\").replace('\'', "\\'");
    let _ = window.eval(&format!(
        "window.studioError && window.studioError('{safe}')"
    ));
}
