//! Windows 持久 IO 探针 —— child-writer(跨进程硬杀崩溃一致性验证的"被杀方")
//!
//! 诚实边界:真正的掉电级 durability(断电/内核崩溃后数据仍在)在纯软件环境
//! **无法证明** —— 进程被杀后数据仍留在 OS page cache,杀进程并不能证明
//! FlushFileBuffers 已把字节落到稳定存储。故本 probe 只认证可真实观测的两点:
//!   1. 原子 rename:tmp -> final 在同卷上原子完成;
//!   2. 跨进程崩溃一致性:writer 进程在 flush + rename 后、正常退出前被父进程
//!      **硬杀**,一个独立进程仍能读到完整且正确(含本 writer 自报 nonce)的
//!      final 文件。
//! 掉电级 durability 与"父目录 fsync 实际生效"由 driver 如实标 BLOCKED_UNCERTIFIED
//! (非 gating),绝不冒充已认证。
//!
//! 本二进制即"被硬杀的 writer 子进程":写入 -> FlushFileBuffers(sync_all)->
//! 原子 rename -> 打印结构化 CHILD_READY 行(含自报 PID / nonce / 实测 flush 结果)
//! -> 挂起等待父进程硬杀;超时自保退出,避免父进程异常时子进程泄漏为常驻孤儿。

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::Path;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// splitmix64:无外部 RNG 依赖,由「时间纳秒 ⊕ pid」播种,产生一次性 nonce。
/// nonce 让 driver 能证明读到的 final 文件确为**本次** writer 所写(新鲜度),
/// 而非上一轮遗留的陈旧文件。
fn gen_nonce() -> (u64, u64) {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let mut seed = nanos ^ ((std::process::id() as u64) << 17);
    let mut next = || {
        seed = seed.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = seed;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    };
    (next(), next())
}

/// child-writer 主体:写 -> flush -> 原子 rename -> 打印 READY -> 挂起等被杀。
/// 返回 Err 时由 main 打印 CHILD_ERROR 并以非 0 退出(driver 据此判 ERROR)。
fn run_child(dir: &Path) -> io::Result<()> {
    fs::create_dir_all(dir)?;
    let tmp_path = dir.join("probe_tmp.dat");
    let final_path = dir.join("probe_final.dat");

    // spawn 前 driver 已删旧 final;child 再删一次,保证新鲜度断言严格成立。
    let _ = fs::remove_file(&final_path);
    let _ = fs::remove_file(&tmp_path);

    let (n1, n2) = gen_nonce();
    let nonce_hex = format!("{:016x}{:016x}", n1, n2);
    // 固定前缀 + nonce:driver 逐字节比对整体,并用 nonce 判新鲜度。
    let payload = format!("DURABLE_IO_PROBE_v2\nnonce={}\n", nonce_hex).into_bytes();

    // Step 1:写入 tmp。
    let mut f = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&tmp_path)?;
    f.write_all(&payload)?;

    // Step 2:flush + sync_all(Windows 上即 FlushFileBuffers)。实测结果,非硬编码。
    f.flush()?;
    let flush_ok = f.sync_all().is_ok();
    drop(f);

    // Step 3:原子 rename(同卷 MoveFileEx 语义)。
    fs::rename(&tmp_path, &final_path)?;
    let rename_ok = final_path.exists() && !tmp_path.exists();

    // Step 4:尝试 sync 父目录。Windows 上对目录句柄 sync_all 基本是 no-op,
    // 如实上报实测调用结果,不冒充"父目录已持久化"。
    let dir_sync_ok = File::open(dir).and_then(|d| d.sync_all()).is_ok();

    // Step 5:打印结构化 READY 行并**立即 flush stdout**,让 driver 精确拿到
    // writer 自身 PID,据此做定点硬杀与新鲜度校验。
    println!(
        "CHILD_READY pid={} nonce={} bytes={} flush_ok={} rename_ok={} dir_sync_ok={}",
        std::process::id(),
        nonce_hex,
        payload.len(),
        flush_ok,
        rename_ok,
        dir_sync_ok
    );
    io::stdout().flush()?;

    // Step 6:挂起等待父进程硬杀。设 60s 自保上限:父进程异常未杀时子进程自行
    // 退出,绝不泄漏为常驻孤儿进程。
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(60) {
        std::thread::sleep(Duration::from_millis(150));
    }
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    // 调用协议:windows_durable_io child <tmp_dir>
    if args.len() >= 3 && args[1] == "child" {
        match run_child(Path::new(&args[2])) {
            Ok(()) => {}
            Err(e) => {
                eprintln!("CHILD_ERROR {}", e);
                std::process::exit(2);
            }
        }
        return;
    }
    eprintln!("usage: windows_durable_io child <tmp_dir>");
    std::process::exit(64);
}
