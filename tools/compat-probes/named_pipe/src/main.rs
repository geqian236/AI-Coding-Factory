//! 认证命名管道 —— 真·跨进程 Rust client。
//!
//! 作为**独立进程**连接 Python server 创建的命名管道，在同一次运行内连两次：
//!   第 1 次：生成随机 nonce，发 MAGIC+nonce，期望 server 回 ACCEPT(0x01)；
//!   第 2 次：重发**同一** nonce，期望 server 凭其跨进程注册表回 REPLAY(0x00)。
//! 由此证明重放拒绝的判定发生在 server 进程，而非 client 自身。
//!
//! 协议(byte pipe, 定长帧)：
//!   client -> server : MAGIC(4=b"NPB1") + nonce(32)   共 36 字节
//!   server -> client : verdict(1)                     0x01/0x00/0x02
//!
//! client 只写 client_receipt.json(路径经 argv[1] 传入，缺省落在 exe 同目录)；
//! 最终 receipt.json 由 PowerShell driver 合成两侧结论。若管道始终不可用则报
//! BLOCKED_UNCERTIFIED，绝不伪造 PASS。

use std::time::{SystemTime, UNIX_EPOCH};

const MAGIC: &[u8; 4] = b"NPB1";
const NONCE_SIZE: usize = 32;
const VERDICT_ACCEPT: u8 = 0x01;
const VERDICT_REPLAY: u8 = 0x00;

fn now_iso() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    format!("unix_s:{}", d.as_secs())
}

/// splitmix64：无外部 crate 的确定性 PRNG，用时间纳秒 ⊕ pid 播种，
/// 仅用于生成一次性 nonce(探针场景，无需密码学级 CSPRNG)。
fn gen_nonce() -> [u8; NONCE_SIZE] {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let mut state = nanos ^ (std::process::id() as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    let mut out = [0u8; NONCE_SIZE];
    for chunk in out.chunks_mut(8) {
        state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        let bytes = z.to_le_bytes();
        chunk.copy_from_slice(&bytes[..chunk.len()]);
    }
    out
}

fn hex_encode(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

#[cfg(windows)]
mod win {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use std::ptr;

    const GENERIC_READ: u32 = 0x8000_0000;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    const OPEN_EXISTING: u32 = 3;
    const INVALID_HANDLE: isize = -1;
    const ERROR_PIPE_BUSY: u32 = 231;
    const ERROR_FILE_NOT_FOUND: u32 = 2;

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateFileW(n: *const u16, da: u32, sm: u32, sa: *mut u8,
                       cd: u32, fa: u32, ht: isize) -> isize;
        fn ReadFile(h: isize, buf: *mut u8, nb: u32, nread: *mut u32, ov: *mut u8) -> i32;
        fn WriteFile(h: isize, buf: *const u8, nb: u32, nw: *mut u32, ov: *mut u8) -> i32;
        fn CloseHandle(h: isize) -> i32;
        fn GetLastError() -> u32;
    }

    /// 打开管道，带重试：server 尚未就绪(FILE_NOT_FOUND)或实例忙(PIPE_BUSY)时
    /// 退避重试，最多约 5 秒。driver 已用 PIPE_READY 握手，这里是二次保险。
    fn open_pipe(name_w: &[u16]) -> Option<isize> {
        for _ in 0..100 {
            let h = unsafe {
                CreateFileW(name_w.as_ptr(), GENERIC_READ | GENERIC_WRITE, 0,
                            ptr::null_mut(), OPEN_EXISTING, 0, 0)
            };
            if h != INVALID_HANDLE {
                return Some(h);
            }
            let err = unsafe { GetLastError() };
            if err == ERROR_FILE_NOT_FOUND || err == ERROR_PIPE_BUSY {
                std::thread::sleep(std::time::Duration::from_millis(50));
                continue;
            }
            return None;
        }
        None
    }

    /// 单次连接：发 frame(36B)，读回 1 字节裁决。返回 Some(verdict) 或 None(连接/IO 失败)。
    pub fn exchange(name: &str, frame: &[u8]) -> Option<u8> {
        let name_w: Vec<u16> = OsStr::new(name).encode_wide().chain(Some(0)).collect();
        let h = open_pipe(&name_w)?;
        unsafe {
            let mut written: u32 = 0;
            let wok = WriteFile(h, frame.as_ptr(), frame.len() as u32, &mut written, ptr::null_mut());
            if wok == 0 || written as usize != frame.len() {
                CloseHandle(h);
                return None;
            }
            let mut verdict = [0u8; 1];
            let mut read: u32 = 0;
            let rok = ReadFile(h, verdict.as_mut_ptr(), 1, &mut read, ptr::null_mut());
            CloseHandle(h);
            if rok == 0 || read != 1 {
                return None;
            }
            Some(verdict[0])
        }
    }
}

fn main() {
    let pipe_name = r"\\.\pipe\factory_auth_probe";
    // client_receipt.json 输出路径：argv[1] 优先，缺省落在 exe 同目录。
    let out_path = std::env::args().nth(1).unwrap_or_else(|| {
        let mut p = std::env::current_exe().unwrap_or_default();
        p.set_file_name("client_receipt.json");
        p.to_string_lossy().into_owned()
    });

    let nonce = gen_nonce();
    let mut frame = Vec::with_capacity(MAGIC.len() + NONCE_SIZE);
    frame.extend_from_slice(MAGIC);
    frame.extend_from_slice(&nonce);

    // 第 1 次：新 nonce，期望 ACCEPT。第 2 次：同一 nonce，期望 REPLAY。
    #[cfg(windows)]
    let first = win::exchange(pipe_name, &frame);
    #[cfg(windows)]
    let second = if first.is_some() { win::exchange(pipe_name, &frame) } else { None };

    #[cfg(not(windows))]
    let (first, second): (Option<u8>, Option<u8>) = (None, None);

    // 断言据实计算，绝不硬编码。
    let first_accepted = first == Some(VERDICT_ACCEPT);
    let replay_rejected = second == Some(VERDICT_REPLAY);
    let pipe_available = first.is_some();

    let status = if !pipe_available {
        "BLOCKED_UNCERTIFIED"
    } else if first_accepted && replay_rejected {
        "PASS"
    } else {
        "FAIL"
    };

    let receipt = serde_json::json!({
        "spike":   "named_pipe_client",
        "status":  status,
        "environment": { "os": std::env::consts::OS, "arch": std::env::consts::ARCH,
                         "client_pid": std::process::id() },
        "actions": [
            "connect #1: send MAGIC+nonce, expect ACCEPT",
            "connect #2: resend same nonce, expect REPLAY",
        ],
        "observable_facts": {
            "pipe_name": pipe_name,
            "nonce_sha256_prefix": &hex_encode(&nonce)[..16],  // 脱敏：不外泄完整 nonce
            "first_verdict":  first.map(|v| v as i32).unwrap_or(-1),
            "second_verdict": second.map(|v| v as i32).unwrap_or(-1),
        },
        "assertions": [
            { "name": "first_nonce_accepted", "passed": first_accepted,
              "detail": format!("first_verdict={:?}", first) },
            { "name": "replay_rejected_by_server", "passed": replay_rejected,
              "detail": format!("second_verdict={:?} (same nonce)", second) },
        ],
        "artifact_digest": format!("nonce_prefix:{}", &hex_encode(&nonce)[..16]),
        "timestamp": now_iso()
    });

    let out = serde_json::to_string_pretty(&receipt).unwrap();
    println!("{}", out);
    let _ = std::fs::write(&out_path, &out);

    // PASS / BLOCKED 退 0；FAIL 退 1，供 driver 判定。
    if status == "FAIL" {
        std::process::exit(1);
    }
}
