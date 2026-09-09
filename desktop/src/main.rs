// Fenris desktop shell.
//
// Per the Tauri v2 packaging research (Tauri 2.11.5, verified against
// plugin source and docs):
// - The Python engine is a PyInstaller ONEDIR sidecar (externalBin;
//   onefile has a documented uvicorn Ctrl+C bug, so onedir is the
//   research verdict). The bundle ships it; the updater replaces the
//   whole bundle, so engine updates ship as app updates.
// - The web UI loads from the bundled static frontendDist; the shell
//   keeps the CSP's connect-src path open to the sidecar's localhost
//   port - the documented pattern for local-API apps.
// - The sidecar gets FENRIS_HOME under the OS app-data dir so the
//   packaged app never writes into the bundle directory.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            use tauri::Manager;
            use tauri_plugin_shell::process::CommandEvent;
            use tauri_plugin_shell::ShellExt;

            let app_data = app.path().app_data_dir()?;
            std::fs::create_dir_all(&app_data)?;
            let fenris_home = app_data.join("engine-data");

            // The engine sidecar: PyInstaller onedir binary, named with
            // the target triple in the bundle (fenris-engine-x86_64-...).
            let sidecar = app
                .shell()
                .sidecar("fenris-engine")
                .expect("fenris-engine sidecar missing from bundle");

            let (mut _rx, _child) = sidecar
                .args(["api", "--port", "8714"])
                .env("FENRIS_HOME", fenris_home)
                .env("FENRIS_PORT", "8714")
                .spawn()
                .expect("failed to spawn fenris-engine sidecar");

            // Drain sidecar output so the pipe never blocks the engine.
            tauri::async_runtime::spawn(async move {
                while let Some(event) = _rx.recv().await {
                    if let CommandEvent::Stderr(line) = event {
                        eprintln!("[fenris-engine] {}", String::from_utf8_lossy(&line));
                    }
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Fenris desktop");

    let _ = app;
}
