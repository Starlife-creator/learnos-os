# 0006 测试入口、临时目录与日志隔离

状态：生效

## 决策
- `python run_tests.py` 是唯一测试入口（包模式 `-t .`），确保加载 `tests/__init__.py` 的 shim。
- 测试临时数据只放 `tests/.tmp/`。shim 在 Windows 上让 `tempfile.mkdtemp` 不带 `0o700` 创建目录。
- 临时目录清理失败不打断用例，但记录到 `tests.LEFTOVER_DIRS`；`run_tests.py` 结束时重试删除并打印残留。
- 测试通过 `LEARNOS_LOG` 把日志写到 `tests/.tmp/test.log`，不碰真实 `learnos.log`。

## 原因
- Python 3.13 起，Windows 上 `os.mkdir(path, 0o700)` 会生成仅属主可访问的 ACL。在受限令牌（沙箱）下，这种目录既不能写也不能删，曾在 `tests/.tmp` 堆积 3400 多个孤儿目录，而旧 shim 静默吞掉了清理错误。
- 测试迁移日志曾占满 1MB×3 的滚动日志，真实运行记录被冲掉。

## 后果
- 新测试用 `tempfile.TemporaryDirectory(dir=tests/.tmp)` 即可，不需要自己处理清理失败。
- 跑完出现残留提示时要排查（多半是 SQLite 连接没关）。
