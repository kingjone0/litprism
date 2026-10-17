//! 文献棱镜 LitPrism · Tauri 桌面外壳
//!
//! 职责边界很窄，但每一条都有原因：
//!
//! 1. **单实例** —— 两个实例会各起一个 sidecar，抢同一个知识库（SQLite 写锁）。
//! 2. **sidecar 生命周期** —— 启动 Python 后台、读 stdout 握手行、退出时回收。
//! 3. **把窗口导航到后台提供的页面** —— 见下面「为什么不用内嵌前端」。
//!
//! 这里**不碰业务**：解读、核验、检索全在 Python 侧。外壳只做进程与窗口。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent};

// ─────────────────────────────────────────────────────────────────────────────
// 为什么窗口直接加载 sidecar 的页面，而不是把前端打进 Tauri 再跨源调 API？
//
// 曾按官方常见做法实现过：前端打进包内（origin = tauri://localhost），
// 通过 initialization_script / invoke 注入 {port, token}，再靠 CORS 跨源访问 127.0.0.1。
// 实测代价很大且难排障：
//   · 页面里引用的是绝对路径 /static/app.js，与 frontendDist 的根目录对不上，
//     要么镜像一份目录、要么改页面，两边迟早漂移；
//   · 跨源要 CORS + 预检 + 自定义头，还叠加 CSP 对注入脚本的限制；
//   · 出问题时窗口是白屏，WebView2 的渲染层用 PrintWindow 抓不到，
//     调试端口在本机也拿不到，几乎无法取证。
//
// 现在：sidecar 本来就是这套 UI 的唯一提供方（`run.py serve` 用的就是它）。
// 外壳等握手完成 → 把窗口导航到 http://127.0.0.1:<port>/#t=<token>。
//   · 同源，无 CORS、无预检；
//   · token 放 URL fragment —— fragment 不会发给服务器，也不会进 Referer；
//   · 浏览器模式与桌面模式跑的是同一份前端，不存在第二份副本。
// 代价：启动期间只能先显示 splash（onefile 解包十几秒），这是明账。
// ─────────────────────────────────────────────────────────────────────────────

/// sidecar 打进包后的文件名（与 PyInstaller 产物同名）
const SIDECAR_EXE: &str = "litprism-sidecar.exe";
/// 握手行前缀，与 `src/litprism/sidecar.py` 严格一致
const HANDSHAKE_PREFIX: &str = "LITPRISM_READY ";
/// 冷启动上限：onefile 每次要把 ~140MB 解到临时目录，慢机器十几秒很常见
const STARTUP_TIMEOUT: Duration = Duration::from_secs(120);
/// CREATE_NO_WINDOW —— 不弹控制台
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Debug, Serialize, Deserialize)]
struct SidecarInfo {
    port: u16,
    token: String,
    /// 真实服务进程 PID。onefile 的启动器和真进程是两个 PID，
    /// 只杀启动器会留下孤儿进程占着端口 —— 所以握手行要带上它。
    #[serde(default)]
    pid: Option<u32>,
}

#[derive(Default)]
struct Gate {
    info: Option<SidecarInfo>,
    error: Option<String>,
}

#[derive(Clone)]
struct Sidecar {
    gate: Arc<(Mutex<Gate>, Condvar)>,
    child: Arc<Mutex<Option<Child>>>,
}

impl Sidecar {
    fn new() -> Self {
        Self {
            gate: Arc::new((Mutex::new(Gate::default()), Condvar::new())),
            child: Arc::new(Mutex::new(None)),
        }
    }

    fn publish(&self, info: SidecarInfo) {
        let (lock, cv) = &*self.gate;
        if let Ok(mut g) = lock.lock() {
            g.info = Some(info);
            g.error = None;
        }
        cv.notify_all();
    }

    fn fail(&self, msg: impl Into<String>) {
        let (lock, cv) = &*self.gate;
        if let Ok(mut g) = lock.lock() {
            if g.info.is_none() {
                g.error = Some(msg.into());
            }
        }
        cv.notify_all();
    }

    /// 等到握手完成（或失败 / 超时）。
    fn wait(&self, timeout: Duration) -> Result<SidecarInfo, String> {
        let deadline = Instant::now() + timeout;
        let (lock, cv) = &*self.gate;
        let mut g = lock.lock().map_err(|_| "握手状态锁已损坏".to_string())?;
        loop {
            if let Some(info) = g.info.clone() {
                return Ok(info);
            }
            if let Some(err) = g.error.clone() {
                return Err(err);
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err(format!("后台服务启动超时（{}s）", timeout.as_secs()));
            }
            let (next, _) = cv
                .wait_timeout(g, left.min(Duration::from_millis(250)))
                .map_err(|_| "等待握手时锁已损坏".to_string())?;
            g = next;
        }
    }

    fn store_child(&self, child: Child) {
        if let Ok(mut slot) = self.child.lock() {
            *slot = Some(child);
        }
    }

    fn info_pid(&self) -> Option<u32> {
        let (lock, _) = &*self.gate;
        lock.lock().ok().and_then(|g| g.info.as_ref().and_then(|i| i.pid))
    }

    /// 回收进程。onefile 的启动器与真进程都要杀，否则孤儿进程会占住端口。
    fn shutdown(&self) {
        if let Some(mut child) = self.child.lock().ok().and_then(|mut g| g.take()) {
            let _ = child.kill();
            let _ = child.wait();
        }
        if let Some(pid) = self.info_pid() {
            let mut cmd = Command::new("taskkill");
            cmd.args(["/PID", &pid.to_string(), "/F"])
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            hide_console(&mut cmd).status().ok();
        }
    }
}

/// 让子进程不弹黑框。非 Windows 平台上是空操作（当前只在 Windows 上验证过）。
fn hide_console(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW)
    }
    #[cfg(not(windows))]
    {
        cmd
    }
}

/// 找到 sidecar 可执行文件。顺序：环境变量 → 安装目录 → 开发目录。
fn locate_sidecar(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    if let Ok(p) = std::env::var("LITPRISM_SIDECAR") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Ok(p);
        }
    }

    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(dir) = app.path().resource_dir() {
        candidates.push(dir.join(SIDECAR_EXE));
        candidates.push(dir.join("binaries").join(SIDECAR_EXE));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join(SIDECAR_EXE));
            // 开发态：src-tauri/target/{debug,release}/ → 项目根/packaging/dist
            candidates.push(dir.join("../../../packaging/dist").join(SIDECAR_EXE));
        }
    }

    for c in &candidates {
        if c.is_file() {
            return Ok(c.clone());
        }
    }
    Err(format!(
        "找不到后台服务 {}。请先运行 `python packaging/build.py` 生成它；\
         或用环境变量 LITPRISM_SIDECAR 指定路径。已尝试：{}",
        SIDECAR_EXE,
        candidates
            .iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
            .join(" / ")
    ))
}

/// 启动 sidecar 并持续读取 stdout。握手行之前允许任意其它输出（日志等）。
fn spawn_sidecar(path: &Path, sc: &Sidecar) -> Result<(), String> {
    let mut cmd = Command::new(path);
    cmd.args(["--port", "0"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut child = hide_console(&mut cmd)
        .spawn()
        .map_err(|e| format!("启动后台服务失败（{}）：{e}", path.display()))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "无法读取后台服务的输出".to_string())?;
    sc.store_child(child);

    let sc2 = sc.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines().map_while(Result::ok) {
            if let Some(rest) = line.strip_prefix(HANDSHAKE_PREFIX) {
                match serde_json::from_str::<SidecarInfo>(rest) {
                    Ok(info) => sc2.publish(info),
                    Err(e) => sc2.fail(format!("握手信息解析失败：{e}（原始内容：{rest}）")),
                }
            }
        }
        // stdout 关闭 = 进程没了；若始终没握手，说明它启动即失败
        sc2.fail("后台服务未输出握手信息就退出了");
    });

    Ok(())
}

/// 在页面底部压一条常驻错误条（失败时不能让用户对着空界面猜）。
///
/// 用注入的 DOM 而不是页面里的固定节点：错误可能发生在页面脚本跑起来之前，
/// 也可能页面根本没加载 —— 两种情况都得看得见。
fn show_error(win: &tauri::WebviewWindow, message: &str) {
    let literal = serde_json::to_string(message).unwrap_or_else(|_| "\"\"".to_owned());
    let js = format!(
        r#"(() => {{
            const el = document.createElement('div');
            el.style.cssText = 'position:fixed;left:16px;right:16px;bottom:16px;z-index:2147483647;'
              + 'padding:12px 16px;border-radius:10px;background:#7f1d1d;color:#fff;'
              + 'font:13px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;'
              + 'white-space:pre-wrap;box-shadow:0 8px 24px rgba(0,0,0,.35)';
            el.textContent = '后台服务启动失败：' + {literal};
            if (document.body) document.body.appendChild(el);
        }})()"#
    );
    let _ = win.eval(js);
}

fn main() {
    let sidecar = Sidecar::new();
    let boot = sidecar.clone();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // 第二次启动：把已有窗口提到前面，而不是再起一个后台
            let w = app
                .get_webview_window("app")
                .or_else(|| app.get_webview_window("main"));
            if let Some(w) = w {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .manage(sidecar)
        .setup(move |app| {
            // 窗口写在 tauri.conf.json 里，**不要在代码里创建**。
            // 实测规律（cargo build，不走 tauri CLI）：
            //   · 配置里的窗口 → 正常加载 http://tauri.localhost/；
            //   · WebviewWindowBuilder 在 setup 或 run_on_main_thread 里建的窗口
            //     → 一律停在 about:blank，网络层零请求。
            // 所以窗口由配置来建，这里只负责起后台、等握手、再把地址推给页面。

            // 启动后台
            match locate_sidecar(app.handle()) {
                Ok(path) => {
                    if let Err(e) = spawn_sidecar(&path, &boot) {
                        boot.fail(e);
                    }
                }
                Err(e) => boot.fail(e),
            }

            // 等握手 → 把 {base, token} 推给页面。
            // 页面侧的 api() 会等这次注入（见 app.js 的 ENV_READY），
            // 因此不存在「首屏请求抢跑，拿到 401」的问题。
            let gate = boot.clone();
            let handle = app.handle().clone();
            std::thread::spawn(move || match gate.wait(STARTUP_TIMEOUT) {
                Ok(info) => {
                    let base = format!("http://127.0.0.1:{}", info.port);
                    let token = info.token;
                    let dispatcher = handle.clone();
                    let inner = dispatcher.clone();
                    let _ = dispatcher.run_on_main_thread(move || {
                        if let Some(w) = inner.get_webview_window("main") {
                            // {:?} 给字符串加引号并转义 —— token 是 URL-safe base64，
                            // 但拼 JS 字面量不该靠「它看起来安全」这种假设
                            let js = format!(
                                "window.__setLitPrismEnv && window.__setLitPrismEnv({{ base: {b:?}, token: {t:?} }});",
                                b = base,
                                t = token
                            );
                            if let Err(e) = w.eval(&js) {
                                eprintln!("注入服务地址失败：{e}");
                            }
                        }
                    });
                }
                Err(e) => {
                    let dispatcher = handle.clone();
                    let inner = dispatcher.clone();
                    let _ = dispatcher.run_on_main_thread(move || {
                        if let Some(w) = inner.get_webview_window("main") {
                            show_error(&w, &e);
                        }
                    });
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Tauri 应用初始化失败");

    app.run(|handle, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            handle.state::<Sidecar>().shutdown();
        }
    });
}
