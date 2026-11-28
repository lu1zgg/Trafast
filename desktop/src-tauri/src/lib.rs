//! Trafast 桌面版的原生部分：系统钥匙串存 API 密钥、托盘菜单、屏幕顶部行情条窗口。
//! 界面和所有网络请求都在前端（WebView）里，直接连用户自己部署的服务器。

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, PhysicalPosition, WebviewWindow, WindowEvent,
};

const SERVICE: &str = "Trafast";

fn entry(server: &str) -> Result<keyring::Entry, String> {
    if server.is_empty() || server.len() > 512 {
        return Err("服务器地址不对".into());
    }
    keyring::Entry::new(SERVICE, server).map_err(|e| e.to_string())
}

/// 读这台服务器保存的 API 密钥（macOS 钥匙串 / Windows 凭据管理器）。
#[tauri::command]
fn secret_get(server: String) -> Result<Option<String>, String> {
    match entry(&server)?.get_password() {
        Ok(k) => Ok(Some(k)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
fn secret_set(server: String, key: String) -> Result<(), String> {
    if !key.starts_with("tfk_") || key.len() > 200 {
        return Err("密钥格式不对，应以 tfk_ 开头".into());
    }
    entry(&server)?.set_password(&key).map_err(|e| e.to_string())
}

#[tauri::command]
fn secret_delete(server: String) -> Result<(), String> {
    match entry(&server)?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// 把行情条摆到主窗口所在屏幕（没有就用主屏）的顶部正中，菜单栏 / 任务栏以外的区域里。
fn dock_ticker(app: &AppHandle, w: &WebviewWindow) {
    let monitor = app
        .get_webview_window("main")
        .and_then(|m| m.current_monitor().ok().flatten())
        .or_else(|| w.primary_monitor().ok().flatten());
    let (Some(m), Ok(size)) = (monitor, w.outer_size()) else { return };
    let area = m.work_area();
    let gap = (8.0 * m.scale_factor()).round() as i32;
    let x = area.position.x + (area.size.width as i32 - size.width as i32) / 2;
    let y = area.position.y + gap;
    let _ = w.set_position(PhysicalPosition::new(x, y));
}

fn toggle_ticker_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("ticker") {
        if w.is_visible().unwrap_or(false) {
            let _ = w.hide();
        } else {
            dock_ticker(app, &w);
            let _ = w.show();
        }
    }
}

/// 点屏幕顶部行情条上的标的：叫出主窗口并打开这个标的的详情。
#[tauri::command]
fn show_main_symbol(app: AppHandle, symbol: String) {
    if symbol.is_empty() || symbol.len() > 40 {
        return;
    }
    show_main(&app);
    let _ = app.emit_to("main", "open-symbol", symbol);
}

#[tauri::command]
fn toggle_ticker(app: AppHandle) {
    toggle_ticker_window(&app);
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "打开主窗口", true, None::<&str>)?;
    let ticker = MenuItem::with_id(app, "ticker", "显示 / 隐藏屏幕顶部行情条", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "退出 Trafast", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &ticker, &sep, &quit])?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("Trafast 异动监控")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main(app),
            "ticker" => toggle_ticker_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // 再次打开程序时把已有的窗口叫出来，而不是开第二个
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![secret_get, secret_set, secret_delete, toggle_ticker, show_main_symbol])
        .setup(|app| {
            build_tray(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // 关主窗口 = 收到托盘里继续收通知；关行情条 = 隐藏
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("启动 Trafast 失败")
        .run(|app, event| {
            // macOS：点 Dock 图标时重新显示主窗口
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Reopen { .. } = &event {
                show_main(app);
            }
            let _ = (app, event);
        });
}
