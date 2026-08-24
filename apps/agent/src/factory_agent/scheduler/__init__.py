"""调度层入口。

本包承载 Phase 1 单写者调度组件。具体实现保持在子模块中，避免包导入时
启动线程、打开 SQLite 或触碰 Windows kernel object。
"""
