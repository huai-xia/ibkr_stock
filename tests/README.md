# tests/

功能测试目录。详细规范见项目根目录 `CLAUDE.md`。

## 目录结构

```
tests/
├── README.md
├── __init__.py              # pytest 入口
├── test_connection.py       # 单元测试 (pytest)
├── test_data.py             # 单元测试 (pytest)
└── <feature_name>/          # 功能测试子文件夹
    ├── xxx.py               # 测试脚本
    └── output/              # 输出文件 (不提交)
```

## 与 debug/ 的区别

| | tests/ | debug/ |
|------|--------|--------|
| 用途 | 功能测试 / 临时测试 | Bug 复现 + 修复验证 |
| 触发词 | 「测试XX功能」 | 「调试XX bug」 |

## 使用方式

从项目根目录运行：

```bash
# pytest 单元测试
pytest tests/ -v

# 手动功能测试
python3 tests/<feature_name>/xxx.py
```
