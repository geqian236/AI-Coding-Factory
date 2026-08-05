//! 执行时钟探针
//! 验证：单调时钟包含睡眠时间；捕获 boot_id；测量与壁钟漂移。
//! 输出 JSON receipt。

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn now_unix_nanos() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos()
}

/// 读取 Windows boot 时间（最后一次系统启动）作为 boot_id 代理
/// 通过 GetTickCount64 和 SystemTime 推算
fn get_windows_boot_proxy() -> String {
    // 读取 WMI LastBootUpTime 的近似值：当前时间 - TickCount64
    // 此实现不依赖 wmi crate，仅做基本验证
    let tick_ms = unsafe {
        #[link(name = "kernel32")]
        extern "system" { fn GetTickCount64() -> u64; }
        GetTickCount64()
    };
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    let boot_epoch_ms = now_ms.saturating_sub(tick_ms);
    format!("boot_epoch_ms:{}", boot_epoch_ms)
}

fn main() {
    let sleep_duration_ms: u64 = 100;

    // 测量前的壁钟和单调时钟
    let wall_before_ns = now_unix_nanos();
    let mono_before    = Instant::now();

    // 睡眠
    std::thread::sleep(Duration::from_millis(sleep_duration_ms));

    // 测量后的壁钟和单调时钟
    let wall_after_ns  = now_unix_nanos();
    let mono_elapsed_ns = mono_before.elapsed().as_nanos();

    let wall_elapsed_ns = wall_after_ns.saturating_sub(wall_before_ns);
    let drift_ns = (mono_elapsed_ns as i128) - (wall_elapsed_ns as i128);
    let drift_ppm = if wall_elapsed_ns > 0 {
        drift_ns * 1_000_000 / wall_elapsed_ns as i128
    } else { 0 };

    let mono_includes_sleep = mono_elapsed_ns >= (sleep_duration_ms as u128 * 900_000); // ≥90ms

    let boot_id = get_windows_boot_proxy();

    let receipt = serde_json::json!({
        "spike": "clock_source",
        "status": if mono_includes_sleep { "PASS" } else { "FAIL" },
        "environment": {
            "os":   std::env::consts::OS,
            "arch": std::env::consts::ARCH,
            "probe_version": "0.1.0"
        },
        "actions": [
            format!("record wall_ns={} mono_start", wall_before_ns),
            format!("sleep {}ms", sleep_duration_ms),
            format!("record wall_ns={} mono_elapsed_ns={}", wall_after_ns, mono_elapsed_ns)
        ],
        "observable_facts": {
            "sleep_requested_ms":  sleep_duration_ms,
            "mono_elapsed_ns":     mono_elapsed_ns,
            "wall_elapsed_ns":     wall_elapsed_ns,
            "drift_ns":            drift_ns,
            "drift_ppm":           drift_ppm,
            "mono_includes_sleep": mono_includes_sleep,
            "boot_id":             boot_id
        },
        "assertions": [
            { "name": "monotonic_includes_sleep", "passed": mono_includes_sleep,
              "detail": format!("mono_elapsed={}ns >= {}ms*0.9", mono_elapsed_ns, sleep_duration_ms) },
            { "name": "drift_within_tolerance", "passed": drift_ppm.abs() < 10_000,
              "detail": format!("drift={}ppm (threshold 10000ppm)", drift_ppm) }
        ],
        "artifact_digest": format!("mono_ns:{}", mono_elapsed_ns),
        "timestamp": format!("unix_ns:{}", wall_after_ns)
    });

    println!("{}", serde_json::to_string_pretty(&receipt).unwrap());
    if receipt["status"] != "PASS" { std::process::exit(1); }
}