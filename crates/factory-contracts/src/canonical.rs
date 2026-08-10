//! crates/factory-contracts/src/canonical.rs
//!
//! RFC 8785 (JCS) + Unicode NFC 规范化 —— Rust 实现。
//! 本模块输出的字节序列必须与 Python (factory_agent.policy.canonical_json)
//! 及 TypeScript (packages/factory-contracts/src/canonical.ts) 完全一致，
//! 是所有 hash 的共同底座。
//!
//! 规范化规则（算法版本 canonical-json-v1）：
//!   1. 所有字符串（键与值）先递归做 Unicode NFC 归一化。
//!   2. 对象键按 UTF-16 code unit 序升序排列（RFC 8785 §3.2.3）。
//!      Rust String 默认按 Unicode scalar value（等价 UTF-8 字节）排序，
//!      与 UTF-16 code unit 序在补充平面字符上不同，故必须显式用
//!      `encode_utf16()` 序列比较。
//!   3. 输出无多余空白；字符串按 RFC 8785 §3.2.2.2 最小化转义。
//!   4. 数字按 RFC 8785 §3.2.2.3（ES6 Number::toString）最短可往返序列化：
//!      1 与 1.0 → "1"；-0 → "0"；1e2 → "100"；1.5 → "1.5"；1e-7 → "1e-7"。
//!      仅拒绝 NaN、Infinity、超出 I-JSON 可互操作范围 [-(2^53-1), 2^53-1] 的数字。
//!   5. 解析 JSON 文本时拒绝重复对象键（自定义 serde Visitor 检测）。
//!
//! 失败错误码：canonical-json-error

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use std::collections::HashSet;
use std::fmt;
use unicode_normalization::UnicodeNormalization;

/// 算法版本号：任何影响输出字节的改动都必须同步升级此常量与 golden vectors。
pub const CANONICAL_JSON_VERSION: &str = "canonical-json-v1";

/// I-JSON / RFC 8785 可互操作整数范围：[-(2^53-1), 2^53-1]。
const MAX_SAFE: i64 = 9_007_199_254_740_991; // 2^53 - 1
const MIN_SAFE: i64 = -9_007_199_254_740_991;

/// 规范化失败异常，携带稳定 error_code（不含完整计划正文）。
#[derive(Debug)]
pub struct CanonicalJsonError {
    /// 已脱敏的错误明细。
    pub detail: String,
}

impl CanonicalJsonError {
    /// 稳定错误码。
    pub const ERROR_CODE: &'static str = "canonical-json-error";

    /// 构造错误。
    fn new(detail: impl Into<String>) -> Self {
        Self { detail: detail.into() }
    }
}

impl fmt::Display for CanonicalJsonError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", Self::ERROR_CODE, self.detail)
    }
}

impl std::error::Error for CanonicalJsonError {}

/// 规范化内部中间值：保留对象插入顺序以便检测重复键与做 UTF-16 排序。
#[derive(Debug, Clone)]
pub enum CanonValue {
    /// JSON null。
    Null,
    /// JSON 布尔。
    Bool(bool),
    /// 整数（i64 承载 [-(2^53-1), 2^53-1] 范围内的值）。
    Int(i64),
    /// 浮点（仅整数值可通过 encode，其余拒绝）。
    Float(f64),
    /// 字符串。
    Str(String),
    /// 数组。
    Array(Vec<CanonValue>),
    /// 对象（保留原始键顺序，键的 NFC/排序在 encode 阶段处理）。
    Object(Vec<(String, CanonValue)>),
}

impl From<&serde_json::Value> for CanonValue {
    /// 由 serde_json::Value 构造 CanonValue（用于 plan/event hash 的已解析对象）。
    fn from(v: &serde_json::Value) -> Self {
        match v {
            serde_json::Value::Null => CanonValue::Null,
            serde_json::Value::Bool(b) => CanonValue::Bool(*b),
            serde_json::Value::Number(n) => {
                if let Some(i) = n.as_i64() {
                    CanonValue::Int(i)
                } else if let Some(u) = n.as_u64() {
                    // 超过 i64 的正整数用 f64 承载，范围检查阶段会拒绝。
                    if u <= i64::MAX as u64 {
                        CanonValue::Int(u as i64)
                    } else {
                        CanonValue::Float(u as f64)
                    }
                } else {
                    CanonValue::Float(n.as_f64().unwrap_or(f64::NAN))
                }
            }
            serde_json::Value::String(s) => CanonValue::Str(s.clone()),
            serde_json::Value::Array(a) => {
                CanonValue::Array(a.iter().map(CanonValue::from).collect())
            }
            serde_json::Value::Object(o) => CanonValue::Object(
                o.iter().map(|(k, val)| (k.clone(), CanonValue::from(val))).collect(),
            ),
        }
    }
}

impl<'de> Deserialize<'de> for CanonValue {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct CanonVisitor;

        impl<'de> Visitor<'de> for CanonVisitor {
            type Value = CanonValue;

            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("任意合法 JSON 值")
            }

            fn visit_unit<E>(self) -> Result<CanonValue, E> {
                Ok(CanonValue::Null)
            }

            fn visit_none<E>(self) -> Result<CanonValue, E> {
                Ok(CanonValue::Null)
            }

            fn visit_bool<E>(self, v: bool) -> Result<CanonValue, E> {
                Ok(CanonValue::Bool(v))
            }

            fn visit_i64<E>(self, v: i64) -> Result<CanonValue, E> {
                Ok(CanonValue::Int(v))
            }

            fn visit_u64<E>(self, v: u64) -> Result<CanonValue, E> {
                if v <= i64::MAX as u64 {
                    Ok(CanonValue::Int(v as i64))
                } else {
                    Ok(CanonValue::Float(v as f64))
                }
            }

            fn visit_f64<E>(self, v: f64) -> Result<CanonValue, E> {
                Ok(CanonValue::Float(v))
            }

            fn visit_str<E>(self, v: &str) -> Result<CanonValue, E> {
                Ok(CanonValue::Str(v.to_string()))
            }

            fn visit_string<E>(self, v: String) -> Result<CanonValue, E> {
                Ok(CanonValue::Str(v))
            }

            fn visit_seq<A>(self, mut seq: A) -> Result<CanonValue, A::Error>
            where
                A: SeqAccess<'de>,
            {
                let mut items = Vec::new();
                while let Some(item) = seq.next_element::<CanonValue>()? {
                    items.push(item);
                }
                Ok(CanonValue::Array(items))
            }

            fn visit_map<A>(self, mut map: A) -> Result<CanonValue, A::Error>
            where
                A: MapAccess<'de>,
            {
                let mut items: Vec<(String, CanonValue)> = Vec::new();
                let mut seen: HashSet<String> = HashSet::new();
                // serde_json 的 MapAccess 会依次给出重复键，故此处能检测原始文本重复。
                while let Some((k, v)) = map.next_entry::<String, CanonValue>()? {
                    if !seen.insert(k.clone()) {
                        return Err(de::Error::custom(format!("JSON 文本含重复对象键：'{k}'")));
                    }
                    items.push((k, v));
                }
                Ok(CanonValue::Object(items))
            }
        }

        deserializer.deserialize_any(CanonVisitor)
    }
}

/// 对字符串做 Unicode NFC 归一化。
fn nfc(text: &str) -> String {
    text.nfc().collect()
}

/// 按 RFC 8785 §3.2.2.2 将字符串编码为带引号的 JSON 字面量并追加到 out。
/// 先做 NFC，再逐 code point 最小化转义；非 ASCII 原样输出为 UTF-8。
fn encode_string(out: &mut String, text: &str) {
    let normalized = nfc(text);
    out.push('"');
    for ch in normalized.chars() {
        let code = ch as u32;
        match code {
            0x08 => out.push_str("\\b"),
            0x09 => out.push_str("\\t"),
            0x0A => out.push_str("\\n"),
            0x0C => out.push_str("\\f"),
            0x0D => out.push_str("\\r"),
            0x22 => out.push_str("\\\""),
            0x5C => out.push_str("\\\\"),
            c if c < 0x20 => out.push_str(&format!("\\u{c:04x}")),
            _ => out.push(ch),
        }
    }
    out.push('"');
}

/// 按 ECMAScript Number::toString（RFC 8785 §3.2.2.3）格式化非负有效数字。
/// digit_str 为最短、末位非零的有效数字串（长度 k≥1），n 满足
/// value = int(digit_str) × 10^(n-k)。据 ECMA-262 分四种情形输出：
///   - k ≤ n ≤ 21：digit_str 后补 (n-k) 个 0（纯整数）。
///   - 0 < n ≤ 21：在第 n 位后插入小数点。
///   - -6 < n ≤ 0："0." + (-n) 个 0 + digit_str。
///   - 其余（n>21 或 n≤-6）：指数形式 d1[.d2..dk]e±(n-1)。
fn es6_format(digit_str: &str, n: i64) -> String {
    let k = digit_str.len() as i64;
    if k <= n && n <= 21 {
        // 纯整数：末尾补零。
        let mut s = String::from(digit_str);
        s.push_str(&"0".repeat((n - k) as usize));
        return s;
    }
    if 0 < n && n <= 21 {
        // 在第 n 位后插入小数点（digit_str 全 ASCII 数字，按字节切分安全）。
        let n_us = n as usize;
        return format!("{}.{}", &digit_str[..n_us], &digit_str[n_us..]);
    }
    if -6 < n && n <= 0 {
        return format!("0.{}{}", "0".repeat((-n) as usize), digit_str);
    }
    // 指数形式：尾数首位后接小数点与其余位，指数为 n-1。
    let (first, rest) = digit_str.split_at(1);
    let mantissa = if rest.is_empty() {
        String::from(first)
    } else {
        format!("{first}.{rest}")
    };
    let e = n - 1;
    format!("{mantissa}e{}{}", if e >= 0 { "+" } else { "-" }, e.abs())
}

/// 按 RFC 8785 §3.2.2.3（ES6 Number::toString）序列化数字并追加到 out。
/// 与 Python/TypeScript 字节一致：接受 I-JSON 可互操作范围内的小数/指数，
/// 仅拒绝 NaN/Infinity/超范围。
fn encode_number(out: &mut String, value: &CanonValue) -> Result<(), CanonicalJsonError> {
    match value {
        CanonValue::Int(i) => {
            if *i < MIN_SAFE || *i > MAX_SAFE {
                return Err(CanonicalJsonError::new(
                    "数字超出 I-JSON 可互操作范围 [-(2^53-1), 2^53-1]",
                ));
            }
            // 范围内整数的 ES6 形式即其十进制，无指数。
            out.push_str(&i.to_string());
            Ok(())
        }
        CanonValue::Float(f) => {
            if f.is_nan() {
                return Err(CanonicalJsonError::new("拒绝 NaN"));
            }
            if f.is_infinite() {
                return Err(CanonicalJsonError::new("拒绝 Infinity"));
            }
            if *f < MIN_SAFE as f64 || *f > MAX_SAFE as f64 {
                return Err(CanonicalJsonError::new(
                    "数字超出 I-JSON 可互操作范围 [-(2^53-1), 2^53-1]",
                ));
            }
            // -0.0 与 0.0 统一为 "0"。
            if *f == 0.0 {
                out.push('0');
                return Ok(());
            }
            let sign = if *f < 0.0 { "-" } else { "" };
            // Rust `{:e}` 给出最短可往返尾数（首位后带小数点）与十进制指数 E，
            // 即 abs = D.ddd × 10^E，故有效数字串首位前恰有一位，n = E + 1。
            let sci = format!("{:e}", f.abs());
            let (mantissa, exp_str) = sci
                .split_once('e')
                .expect("Rust {:e} 输出必含 'e'");
            let exp: i64 = exp_str.parse().expect("指数应为整数");
            // 去掉小数点得到有效数字串，再去尾随零（保持最短，n=E+1 不受影响）。
            let raw: String = mantissa.chars().filter(|c| *c != '.').collect();
            let trimmed = raw.trim_end_matches('0');
            let digit_str = if trimmed.is_empty() { "0" } else { trimmed };
            let n = exp + 1;
            out.push_str(sign);
            out.push_str(&es6_format(digit_str, n));
            Ok(())
        }
        _ => unreachable!("encode_number 仅处理 Int/Float"),
    }
}

/// 递归将 CanonValue 编码为规范 JSON 字符串片段（无多余空白）。
fn encode(out: &mut String, value: &CanonValue) -> Result<(), CanonicalJsonError> {
    match value {
        CanonValue::Null => out.push_str("null"),
        CanonValue::Bool(true) => out.push_str("true"),
        CanonValue::Bool(false) => out.push_str("false"),
        CanonValue::Int(_) | CanonValue::Float(_) => encode_number(out, value)?,
        CanonValue::Str(s) => encode_string(out, s),
        CanonValue::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                encode(out, item)?;
            }
            out.push(']');
        }
        CanonValue::Object(items) => encode_object(out, items)?,
    }
    Ok(())
}

/// 编码对象：键做 NFC，按 UTF-16 code unit 序排序后拼接；拒绝重复键。
fn encode_object(
    out: &mut String,
    items: &[(String, CanonValue)],
) -> Result<(), CanonicalJsonError> {
    let mut normalized: Vec<(String, &CanonValue)> = Vec::with_capacity(items.len());
    let mut seen: HashSet<String> = HashSet::new();
    for (raw_key, val) in items {
        let key = nfc(raw_key);
        if !seen.insert(key.clone()) {
            return Err(CanonicalJsonError::new("NFC 归一化后出现重复对象键"));
        }
        normalized.push((key, val));
    }
    // RFC 8785：按 UTF-16 code unit 序列排序（与 Python utf-16-be 字节比较等价）。
    normalized.sort_by(|a, b| a.0.encode_utf16().cmp(b.0.encode_utf16()));

    out.push('{');
    for (i, (k, v)) in normalized.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        encode_string(out, k);
        out.push(':');
        encode(out, v)?;
    }
    out.push('}');
    Ok(())
}

/// 将 CanonValue 规范化为 RFC 8785 UTF-8 字节序列。
pub fn canonicalize(value: &CanonValue) -> Result<Vec<u8>, CanonicalJsonError> {
    let mut out = String::new();
    encode(&mut out, value)?;
    Ok(out.into_bytes())
}

/// 将已解析的 serde_json::Value 规范化为字节序列（用于 plan/event hash）。
pub fn canonicalize_value(value: &serde_json::Value) -> Result<Vec<u8>, CanonicalJsonError> {
    canonicalize(&CanonValue::from(value))
}

/// 解析 JSON 文本（拒绝重复键、NaN、Infinity）后规范化为字节序列。
pub fn canonicalize_json_text(text: &str) -> Result<Vec<u8>, CanonicalJsonError> {
    // serde_json 默认不接受 NaN/Infinity 字面量，会在解析阶段失败；
    // 重复键由 CanonValue 的自定义 Deserialize 检测。
    let value: CanonValue = serde_json::from_str(text)
        .map_err(|e| CanonicalJsonError::new(format!("JSON 解析失败：{e}")))?;
    canonicalize(&value)
}
