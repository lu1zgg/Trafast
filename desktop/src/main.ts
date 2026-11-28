import { mount } from "svelte";
import "./app.css";
import App from "./App.svelte";
import { inTauri } from "./lib/secrets";

// Mac：标题栏是透明叠加的（红绿灯在侧栏里），窗口背景是系统玻璃，网页背景要透明。
// 其他系统 / 浏览器调试：用自带标题栏，背景用柔和渐变代替系统玻璃。
const root = document.documentElement;
const mac = /Mac/i.test(navigator.platform || navigator.userAgent);
root.dataset.platform = mac && inTauri() ? "mac" : "other";
if (mac && inTauri()) root.classList.add("vibrant");

const app = mount(App, { target: document.getElementById("app")! });

export default app;
