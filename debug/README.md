# debug/

按 bug 类型分文件夹的测试套件。每个子文件夹包含该 bug 的测试代码和输出结果。

## 目录结构

```
debug/
├── README.md
├── <bug_name>/                  # 按 bug 类型命名
│   ├── *.py                     # 测试脚本
│   └── output/                  # 输出文件
│       ├── charts/              # 图表
│       └── *.txt                # 文本输出
└── common/                      # 共享工具 (预留)
```

## 已有 bug 子文件夹

### email_duplication_bug/
邮件内容重复 + 股票信息缺失 + 告警爆炸 (74封/天)

| 脚本 | 用途 |
|------|------|
| `analyze_warnings.py` | 全模块回测: 走势图 + 告警标注 + 交易模拟 |
| `simulate_old_emails.py` | 旧系统邮件模拟 (对比基线) |
| `ideal_emails_single.py` | 单股理想邮件原型 |
| `ideal_emails_multi.py` | 多股合并理想邮件原型 |
| `verify_fix.py` | 修复后验证脚本 |

## 使用方式

所有脚本从**项目根目录**运行:

```bash
# 回测并生成走势图
python3 debug/email_duplication_bug/analyze_warnings.py

# 验证修复效果
python3 debug/email_duplication_bug/verify_fix.py
```
