//! WSL Runner Identity 探针 —— broker(非破坏性"孤儿存活"验证的"被杀方")
//!
//! 声称能力:CI runner(broker)进程被杀后,它启动的容器不随之死亡 —— 即容器
//! 生命周期独立于启动它的进程(在 Windows + Docker Desktop/WSL2 上,容器由 daemon
//! 拥有,运行在 WSL2 VM 内,不在 broker 的进程树/Job 内)。
//!
//! 诚实边界:真正的 broker-kill(杀 LxssManager 服务或 dockerd daemon)需要管理员
//! 且会**破坏共享系统**,故不做;改为非破坏性等价验证 —— 真实硬杀"启动容器的
//! broker 进程本身",再由独立进程确认容器仍在运行。daemon/LxssManager 级 kill 由
//! driver 如实标 BLOCKED_UNCERTIFIED(非 gating),绝不冒充已认证。
//!
//! 本二进制即"被硬杀的 broker 子进程":用 driver 传入的容器名(含 nonce)起一个
//! detached 容器 -> 从 `docker run -d` 拿到真实 container_id -> 打印结构化
//! BROKER_READY 行(含自报 PID / name / container_id)-> 挂起等父进程硬杀;
//! 超时自保退出,避免 broker 异常时子进程泄漏为常驻孤儿。
//!
//! 注意:容器清理(docker rm -f)由 driver 负责 —— broker 会被硬杀,无法自行清理,
//! 且"清理不依赖 broker 存活"恰恰印证了容器与 broker 解耦。

use std::process::Command;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn now_ts() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| format!("unix_s:{}", d.as_secs()))
        .unwrap_or_else(|_| "unknown".into())
}

/// broker 主体:起 detached 容器 -> 打印 READY -> 挂起等被杀。
/// 返回 Err(msg) 时由 main 打印 BROKER_ERROR 并非 0 退出(driver 据此判 FAIL/ERROR)。
fn run_broker(container_name: &str) -> Result<(), String> {
    // 起 detached 容器:由 daemon 拥有,不在本进程树内。python:3.12-slim 镜像已由
    // driver 预检确认本地缓存,故此处 `docker run` 不应因拉镜像超时;若仍失败则如实返回错误。
    let run = Command::new("docker")
        .args([
            "run",
            "-d",
            "--name",
            container_name,
            "python:3.12-slim",
            "python3",
            "-c",
            "import time; time.sleep(60)",
        ])
        .output()
        .map_err(|e| format!("docker run spawn error: {}", e))?;

    if !run.status.success() {
        return Err(format!(
            "docker run failed: {}",
            String::from_utf8_lossy(&run.stderr).trim()
        ));
    }

    // `docker run -d` 的 stdout 即容器完整 ID —— 这是"本 broker 确实启动了该容器"的
    // 因果铁证(driver 据此核对存活的容器 ID 与本 broker 自报值一致)。
    let container_id = String::from_utf8_lossy(&run.stdout).trim().to_string();
    if container_id.len() < 12 {
        return Err(format!("container id too short: '{}'", container_id));
    }

    // 打印结构化 READY 行并**立即 flush stdout**,让 driver 精确拿到 broker 自身 PID
    // 与容器 ID,据此做定点硬杀与因果核对。
    println!(
        "BROKER_READY pid={} name={} container_id={} ts={}",
        std::process::id(),
        container_name,
        container_id,
        now_ts()
    );
    use std::io::Write;
    let _ = std::io::stdout().flush();

    // 挂起等待父进程硬杀。设 60s 自保上限(与容器 sleep 60 对齐):父进程异常未杀时
    // broker 自行退出,容器也会随 sleep 结束而停止,driver 的 rm -f 仍能兜底清理。
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(60) {
        std::thread::sleep(Duration::from_millis(150));
    }
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    // 调用协议:runner_identity broker <container_name>
    if args.len() >= 3 && args[1] == "broker" {
        match run_broker(&args[2]) {
            Ok(()) => {}
            Err(e) => {
                eprintln!("BROKER_ERROR {}", e);
                std::process::exit(2);
            }
        }
        return;
    }
    eprintln!("usage: runner_identity broker <container_name>");
    std::process::exit(64);
}
