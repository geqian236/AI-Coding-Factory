//! crates/factory-contracts/src/lib.rs
//!
//! AI Coding Factory Rust 合同库入口。
//! 只 re-export 由 generate.py 生成的类型；
//! Task 3/4 的 canonical/plan/event 算法模块在各自 Task 中单独添加。

pub mod generated;

pub use generated::*;
