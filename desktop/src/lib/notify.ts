// 系统通知：macOS 通知中心 / Windows 操作中心。
import { inTauri } from "./secrets";

let granted: boolean | null = null;

export async function ensurePermission(): Promise<boolean> {
  if (granted !== null) return granted;
  if (inTauri()) {
    const n = await import("@tauri-apps/plugin-notification");
    granted = await n.isPermissionGranted();
    if (!granted) granted = (await n.requestPermission()) === "granted";
  } else if ("Notification" in window) {
    granted = Notification.permission === "granted" || (await Notification.requestPermission()) === "granted";
  } else {
    granted = false;
  }
  return granted;
}

export async function notify(title: string, body: string, sound = true): Promise<void> {
  if (!(await ensurePermission())) return;
  if (inTauri()) {
    const n = await import("@tauri-apps/plugin-notification");
    n.sendNotification({ title, body, ...(sound ? { sound: "default" } : {}) });
  } else {
    new Notification(title, { body, silent: !sound });
  }
}
