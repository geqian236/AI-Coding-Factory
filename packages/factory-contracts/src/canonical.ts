/**
 * packages/factory-contracts/src/canonical.ts
 *
 * RFC 8785 (JCS) + Unicode NFC 规范化 —— TypeScript 实现。
 * 本模块输出的字节序列必须与 Python (factory_agent.policy.canonical_json)
 * 及 Rust (factory_contracts::canonical) 完全一致，是所有 hash 的共同底座。
 *
 * 规范化规则（算法版本 canonical-json-v1）：
 *   1. 所有字符串（键与值）先递归做 Unicode NFC 归一化。
 *   2. 对象键按 UTF-16 code unit 序升序排列；JS 字符串原生即 UTF-16，
 *      默认字符串比较即按 code unit 序，故直接用默认比较器。
 *   3. 输出无多余空白；字符串按 RFC 8785 §3.2.2.2 最小化转义。
 *   4. 数字仅接受「有限整数值且落在 [-(2^53-1), 2^53-1]」者，
 *      统一输出十进制整数字面量：1 与 1.0 → "1"；-0 → "0"；1e2 → "100"。
 *      小数、NaN、Infinity、超范围整数一律拒绝（Phase 0 保守子集）。
 *   5. 解析 JSON 文本时拒绝重复对象键（手写严格解析器）。
 *
 * 失败错误码：canonical-json-error
 */

/** 算法版本号：任何影响输出字节的改动都必须同步升级此常量与 golden vectors。 */
export const CANONICAL_JSON_VERSION = "canonical-json-v1";

/** I-JSON / RFC 8785 可互操作整数范围上界 [-(2^53-1), 2^53-1]。 */
const MAX_SAFE = 9007199254740991; // 2^53 - 1
const MIN_SAFE = -9007199254740991;

/** 规范化失败异常，携带稳定 error_code（不含完整计划正文）。 */
export class CanonicalJsonError extends Error {
  readonly errorCode = "canonical-json-error";
  constructor(detail: string) {
    super(`[canonical-json-error] ${detail}`);
    this.name = "CanonicalJsonError";
  }
}

/** RFC 8785 §3.2.2.2 规定的短转义序列（其余控制字符用 \u00xx）。 */
const SHORT_ESCAPES: Record<number, string> = {
  0x08: "\\b",
  0x09: "\\t",
  0x0a: "\\n",
  0x0c: "\\f",
  0x0d: "\\r",
  0x22: '\\"',
  0x5c: "\\\\",
};

/** 对字符串做 Unicode NFC 归一化。 */
function nfc(text: string): string {
  return text.normalize("NFC");
}

/**
 * 按 RFC 8785 §3.2.2.2 将字符串编码为带引号的 JSON 字面量。
 * 先做 NFC，再逐 code point 最小化转义；非 ASCII 原样输出为 UTF-8。
 */
function encodeString(text: string): string {
  const normalized = nfc(text);
  let out = '"';
  for (const ch of normalized) {
    const code = ch.codePointAt(0) as number;
    const short = SHORT_ESCAPES[code];
    if (short !== undefined) {
      out += short;
    } else if (code < 0x20) {
      out += "\\u" + code.toString(16).padStart(4, "0");
    } else {
      out += ch;
    }
  }
  return out + '"';
}

/**
 * 按 Phase 0 整数值数字规则编码数字。
 * @throws CanonicalJsonError 数字为 NaN/Infinity、非整数值或超出安全范围。
 */
function encodeNumber(value: number): string {
  if (Number.isNaN(value)) {
    throw new CanonicalJsonError("拒绝 NaN");
  }
  if (!Number.isFinite(value)) {
    throw new CanonicalJsonError("拒绝 Infinity");
  }
  if (!Number.isInteger(value)) {
    throw new CanonicalJsonError(
      `Phase 0 仅接受整数值数字，收到非整数（version=${CANONICAL_JSON_VERSION}）`,
    );
  }
  if (value < MIN_SAFE || value > MAX_SAFE) {
    throw new CanonicalJsonError("数字超出 I-JSON 可互操作范围 [-(2^53-1), 2^53-1]");
  }
  // String(-0) === "0"，Number.isInteger(-0) === true，符合规则。
  return String(value);
}

/** 递归将已解析的值编码为规范 JSON 字符串片段（无多余空白）。 */
function encode(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (value === true) {
    return "true";
  }
  if (value === false) {
    return "false";
  }
  const t = typeof value;
  if (t === "number") {
    return encodeNumber(value as number);
  }
  if (t === "string") {
    return encodeString(value as string);
  }
  if (Array.isArray(value)) {
    return "[" + value.map((item) => encode(item)).join(",") + "]";
  }
  if (t === "object") {
    return encodeObject(value as Record<string, unknown>);
  }
  throw new CanonicalJsonError(`不支持的类型：${t}`);
}

/** 编码对象：键做 NFC，按 UTF-16 code unit 序排序后拼接；拒绝重复键。 */
function encodeObject(obj: Record<string, unknown>): string {
  const seen = new Set<string>();
  const items: Array<[string, unknown]> = [];
  for (const rawKey of Object.keys(obj)) {
    const key = nfc(rawKey);
    if (seen.has(key)) {
      throw new CanonicalJsonError("NFC 归一化后出现重复对象键");
    }
    seen.add(key);
    items.push([key, obj[rawKey]]);
  }
  // JS 默认字符串比较即按 UTF-16 code unit 序，与 Python 的 utf-16-be 字节比较等价。
  items.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const parts = items.map(([k, v]) => `${encodeString(k)}:${encode(v)}`);
  return "{" + parts.join(",") + "}";
}

/**
 * 将已解析的值规范化为 RFC 8785 UTF-8 字节序列。
 * @throws CanonicalJsonError 值含非法数字、非法类型或重复键。
 */
export function canonicalize(value: unknown): Uint8Array {
  return new TextEncoder().encode(encode(value));
}

/** 便于测试：返回规范化后的 UTF-8 字符串。 */
export function canonicalizeToString(value: unknown): string {
  return encode(value);
}

// ── 严格 JSON 解析器：拒绝重复键、NaN、Infinity ────────────────────────────
// JS 内置 JSON.parse 会静默保留重复键的最后一个值，无法满足合同要求，
// 故手写最小递归下降解析器。NaN/Infinity 不是合法 JSON token，天然被拒绝。

class StrictParser {
  private i = 0;
  constructor(private readonly s: string) {}

  parse(): unknown {
    this.skipWs();
    const value = this.parseValue();
    this.skipWs();
    if (this.i !== this.s.length) {
      throw new CanonicalJsonError("JSON 文本尾部存在多余内容");
    }
    return value;
  }

  private skipWs(): void {
    while (this.i < this.s.length && " \t\n\r".includes(this.s[this.i])) {
      this.i++;
    }
  }

  private parseValue(): unknown {
    this.skipWs();
    if (this.i >= this.s.length) {
      throw new CanonicalJsonError("JSON 文本意外结束");
    }
    const ch = this.s[this.i];
    if (ch === "{") return this.parseObject();
    if (ch === "[") return this.parseArray();
    if (ch === '"') return this.parseString();
    if (ch === "-" || (ch >= "0" && ch <= "9")) return this.parseNumber();
    if (this.s.startsWith("true", this.i)) {
      this.i += 4;
      return true;
    }
    if (this.s.startsWith("false", this.i)) {
      this.i += 5;
      return false;
    }
    if (this.s.startsWith("null", this.i)) {
      this.i += 4;
      return null;
    }
    // NaN / Infinity / -Infinity 等非法 token 走到这里被拒绝。
    throw new CanonicalJsonError(`非法 JSON token（位置 ${this.i}）`);
  }

  private parseObject(): Record<string, unknown> {
    this.i++; // 跳过 '{'
    const result: Record<string, unknown> = {};
    const seen = new Set<string>();
    this.skipWs();
    if (this.s[this.i] === "}") {
      this.i++;
      return result;
    }
    for (;;) {
      this.skipWs();
      if (this.s[this.i] !== '"') {
        throw new CanonicalJsonError("对象键必须为字符串");
      }
      const key = this.parseString();
      if (seen.has(key)) {
        throw new CanonicalJsonError(`JSON 文本含重复对象键：'${key}'`);
      }
      seen.add(key);
      this.skipWs();
      if (this.s[this.i] !== ":") {
        throw new CanonicalJsonError("对象键后缺少 ':'");
      }
      this.i++;
      result[key] = this.parseValue();
      this.skipWs();
      const sep = this.s[this.i];
      if (sep === ",") {
        this.i++;
        continue;
      }
      if (sep === "}") {
        this.i++;
        return result;
      }
      throw new CanonicalJsonError("对象缺少 ',' 或 '}'");
    }
  }

  private parseArray(): unknown[] {
    this.i++; // 跳过 '['
    const result: unknown[] = [];
    this.skipWs();
    if (this.s[this.i] === "]") {
      this.i++;
      return result;
    }
    for (;;) {
      result.push(this.parseValue());
      this.skipWs();
      const sep = this.s[this.i];
      if (sep === ",") {
        this.i++;
        continue;
      }
      if (sep === "]") {
        this.i++;
        return result;
      }
      throw new CanonicalJsonError("数组缺少 ',' 或 ']'");
    }
  }

  private parseString(): string {
    this.i++; // 跳过起始 '"'
    let out = "";
    for (;;) {
      if (this.i >= this.s.length) {
        throw new CanonicalJsonError("字符串未闭合");
      }
      const ch = this.s[this.i++];
      if (ch === '"') {
        return out;
      }
      if (ch === "\\") {
        const esc = this.s[this.i++];
        switch (esc) {
          case '"': out += '"'; break;
          case "\\": out += "\\"; break;
          case "/": out += "/"; break;
          case "b": out += "\b"; break;
          case "f": out += "\f"; break;
          case "n": out += "\n"; break;
          case "r": out += "\r"; break;
          case "t": out += "\t"; break;
          case "u": {
            const hex = this.s.slice(this.i, this.i + 4);
            if (!/^[0-9a-fA-F]{4}$/.test(hex)) {
              throw new CanonicalJsonError("非法 \\u 转义");
            }
            this.i += 4;
            out += String.fromCharCode(parseInt(hex, 16));
            break;
          }
          default:
            throw new CanonicalJsonError(`非法转义 \\${esc}`);
        }
      } else {
        out += ch;
      }
    }
  }

  private parseNumber(): number {
    const start = this.i;
    if (this.s[this.i] === "-") this.i++;
    while (this.i < this.s.length && /[0-9]/.test(this.s[this.i])) this.i++;
    if (this.s[this.i] === ".") {
      this.i++;
      while (this.i < this.s.length && /[0-9]/.test(this.s[this.i])) this.i++;
    }
    if (this.s[this.i] === "e" || this.s[this.i] === "E") {
      this.i++;
      if (this.s[this.i] === "+" || this.s[this.i] === "-") this.i++;
      while (this.i < this.s.length && /[0-9]/.test(this.s[this.i])) this.i++;
    }
    const token = this.s.slice(start, this.i);
    const value = Number(token);
    if (Number.isNaN(value)) {
      throw new CanonicalJsonError(`非法数字字面量：${token}`);
    }
    return value;
  }
}

/**
 * 解析 JSON 文本（拒绝重复键、NaN、Infinity）后规范化为字节序列。
 * @throws CanonicalJsonError JSON 非法、含重复键或非法数字。
 */
export function canonicalizeJsonText(text: string): Uint8Array {
  const parsed = new StrictParser(text).parse();
  return canonicalize(parsed);
}

/** 便于测试：解析并规范化为字符串。 */
export function canonicalizeJsonTextToString(text: string): string {
  const parsed = new StrictParser(text).parse();
  return encode(parsed);
}
