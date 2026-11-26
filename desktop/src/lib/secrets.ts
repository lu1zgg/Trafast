// API 密钥存在系统钥匙串：macOS 钥匙串 / Windows 凭据管理器（Rust 端用 keyring 实现）。
// 在普通浏览器里调试界面时没有 Tauri，退回到 sessionStorage（关掉就没了）。
import { invoke } from "@tauri-apps/api/core";

export const inTauri = (): boolean => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export async function getKey(server: string): Promise<string | null> {
  if (!server) return null;
  if (!inTauri()) return sessionStorage.getItem("trafast.key." + server);
  return (await invoke<string | null>("secret_get", { server })) ?? null;
}

export async function setKey(server: string, key: string): Promise<void> {
  if (!inTauri()) {
    sessionStorage.setItem("trafast.key." + server, key);
    return;
  }
  await invoke("secret_set", { server, key });
}

export async function deleteKey(server: string): Promise<void> {
  if (!server) return;
  if (!inTauri()) {
    sessionStorage.removeItem("trafast.key." + server);
    return;
  }
  await invoke("secret_delete", { server });
}
