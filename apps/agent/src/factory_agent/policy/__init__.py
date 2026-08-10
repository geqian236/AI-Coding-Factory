"""factory_agent.policy — 跨语言合同策略算法。

包含 RFC 8785 (JCS) + Unicode NFC canonical JSON、计划身份哈希
（semanticPlanHash / planRevisionDigest）与 barrier identity。

这些算法必须与 TypeScript、Rust 实现产生**字节级一致**的输出，
因此所有数字/字符串/排序规则都以版本化 golden vectors 冻结。
"""
