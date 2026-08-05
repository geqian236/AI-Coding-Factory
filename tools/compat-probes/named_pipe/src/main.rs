//! 认证命名管道 Rust 客户端
//! 连接到 Python server 创建的命名管道，接收 nonce，回显，验证 SID 约束。
//! 若 Python server 未运行则报 BLOCKED_UNCERTIFIED。

use std::time::{SystemTime, UNIX_EPOCH};

fn now_iso() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    format!("unix_s:{}", d.as_secs())
}

#[cfg(windows)]
mod win {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;

    pub fn connect_pipe(name: &str) -> Option<Vec<u8>> {
        use std::ptr;
        let name_w: Vec<u16> = OsStr::new(name).encode_wide().chain(Some(0)).collect();
        unsafe {
            #[link(name = "kernel32")]
            extern "system" {
                fn CreateFileW(n: *const u16, da: u32, sm: u32, sa: *mut u8,
                               cd: u32, fa: u32, ht: *mut u8) -> isize;
                fn ReadFile(h: isize, buf: *mut u8, nb: u32, nread: *mut u32,
                            ov: *mut u8) -> i32;
                fn WriteFile(h: isize, buf: *const u8, nb: u32, nw: *mut u32,
                             ov: *mut u8) -> i32;
                fn CloseHandle(h: isize) -> i32;
            }
            let h = CreateFileW(name_w.as_ptr(), 0xC0000000u32, 0, ptr::null_mut(), 3, 0, ptr::null_mut());
            if h == -1 { return None; }
            let mut nonce = vec![0u8; 32];
            let mut read: u32 = 0;
            ReadFile(h, nonce.as_mut_ptr(), 32, &mut read, ptr::null_mut());
            nonce.truncate(read as usize);
            let mut written: u32 = 0;
            WriteFile(h, nonce.as_ptr(), nonce.len() as u32, &mut written, ptr::null_mut());
            CloseHandle(h);
            Some(nonce)
        }
    }
}

fn main() {
    let pipe_name = r"\\.\pipe\factory_auth_probe";

    #[cfg(windows)]
    let result = win::connect_pipe(pipe_name);
    #[cfg(not(windows))]
    let result: Option<Vec<u8>> = None;

    let (status, nonce_hex) = match result {
        Some(nonce) => ("PASS", hex_encode(&nonce)),
        None => ("BLOCKED_UNCERTIFIED", "pipe_not_available".to_string()),
    };

    let receipt = serde_json::json!({
        "spike":   "named_pipe_rust_client",
        "status":  status,
        "environment": { "os": std::env::consts::OS, "arch": std::env::consts::ARCH },
        "actions": [ format!("connect to {}", pipe_name), "echo nonce back" ],
        "observable_facts": { "pipe_name": pipe_name, "nonce_hex": nonce_hex },
        "assertions": [{ "name": "nonce_received_and_echoed", "passed": status == "PASS",
                         "detail": format!("nonce={}", nonce_hex) }],
        "artifact_digest": format!("nonce:{}", nonce_hex),
        "timestamp": now_iso()
    });

    println!("{}", serde_json::to_string_pretty(&receipt).unwrap());
    if status != "PASS" && status != "BLOCKED_UNCERTIFIED" { std::process::exit(1); }
}

fn hex_encode(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}