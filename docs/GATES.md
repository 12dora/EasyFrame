# 代码气味门禁

EasyFrame 把一套 Python 代码气味门禁写在本仓,下游(EasyTrade / EasyCustoms)以 git submodule 消费 `backend/`,因此这里定义的规则会被每一宿主继承。

## 门禁组成

| 门禁 | 做什么 | 阈值 |
|---|---|---|
| ruff 风格 | E/F/I/N/UP/B、ERA001、T201、TD002/003、FIX001/002、RUF100 | 必须清零 |
| ruff 规模 | C901 圈复杂度、PLR0915 语句数、PLR0912 分支、PLR0911 return、PLR1702 嵌套块、PLR0917 位置参数、PLR0913 参数个数 | 见下表;存量冻结在基线 |
| 体积 | 生产文件 / 路由文件 / 测试文件 / fixture / 函数 / 迁移函数 / 测试函数 SLOC | 500 / 180 / 400 / 400 / 40 / 60 / 80 |
| 重复 | 跨文件、规范化后 ≥40 行的滑动窗口 | 命中即违规,按重复区长度棘轮 |

规模阈值(ruff):圈复杂度 10、语句 40、分支 12、return 8、嵌套块 5、位置参数 8、参数 8。`PLR1702` 在 ruff 0.16.2 仍是 preview,片段里打开了 `lint.preview` + `explicit-preview-rules`(避免 E/F/B 前缀把未稳定规则带进来)。

测试目录豁免 PLR0915/PLR0913;`**/api/**/*.py` 豁免 B008/PLR0913(FastAPI `Depends`);种子/清库/预检脚本豁免 T201;迁移 `upgrade()` 豁免 PLR0915。

## 下游怎么接

在宿主 `backend/pyproject.toml`:

```toml
[tool.ruff]
extend = "easyframe/backend/ruff-gates.toml"
```

跑棘轮(体积 + 重复 + 规模类 ruff,对照宿主自己的基线):

```bash
python -m tools.quality_gates.runner --repo <仓库根> --config <gates.json>
```

把 EasyFrame 的 `backend/tools/quality_gates/` 放进 `PYTHONPATH`(镜像里与 `enterprise_platform` 一样 COPY 到原路径即可)。`gates.json` 里写扫描根、阈值表、基线路径;缺省阈值与上表相同。

本仓:`make lint` = `ruff check`(忽略棘轮规则) + `ruff format --check` + runner;`make lint-fix` = RUF100 `--fix` + `ruff format`。

## 棘轮

`.code-smells-baseline.json` 按「规则 → 文件 → 违规数值降序列表」冻结存量。新违规或同一位置数值变差 → 红;变好 → 绿,并提示 `tighten with --update`。`--update` 只允许收缩,拒绝把基线写大。

**抬高阈值或加 `# noqa` 必须写明理由。** 欠账靠拆函数和删重复还,不靠放宽门禁。
