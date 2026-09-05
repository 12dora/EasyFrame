# 代码气味门禁

EasyFrame 把一套 Python 代码气味门禁写在本仓,下游(EasyTrade / EasyCustoms)以 git submodule 消费 `backend/`,因此这里定义的规则会被每一宿主继承。

## 门禁组成

| 门禁 | 做什么 | 阈值 |
|---|---|---|
| ruff 风格 | E/F/I/N/UP/B、ERA001、T201、TD002/003、FIX001/002、RUF100 | 必须清零 |
| ruff 规模 | C901 圈复杂度、PLR0915 语句数、PLR0912 分支、PLR0911 return、PLR1702 嵌套块、PLR0917 位置参数、PLR0913 参数个数 | 见下表;存量冻结在基线 |
| 体积 | 生产文件 / 路由文件 / 测试文件 / fixture / 函数 / 迁移函数 / 测试函数 SLOC | 500 / 180 / 400 / 400 / 40 / 60 / 80 |
| 重复 | 跨文件、规范化后 ≥40 行的滑动窗口 | 命中即违规,按「文件对」的重复行总数棘轮 |

规模阈值(ruff):圈复杂度 10、语句 40、分支 12、return 8、嵌套块 5、位置参数 8、参数 8。`PLR1702` 在 ruff 0.16.2 仍是 preview,片段里打开了 `lint.preview` + `explicit-preview-rules`(避免 E/F/B 前缀把未稳定规则带进来)。

## 下游怎么接

在宿主 `backend/pyproject.toml` **必须** `extend` 共享片段,并**自己声明目录型** `per-file-ignores`。ruff 把 glob 相对「写出该 glob 的配置文件所在目录」解析,不会随 `extend` 改锚到宿主;因此 `ruff-gates.toml` 里只有规则/阈值和 basename 模式(`seed*.py` / `wipe*.py` / `*preflight*.py` / `runner.py`)。像 `**/api/**/*.py` 这种写在片段里的模式永远对不准下游的 `app/api`。

EasyTrade 示例:

```toml
[tool.ruff]
extend = "easyframe/backend/ruff-gates.toml"

[tool.ruff.lint.extend-per-file-ignores]
"app/api/v1/**/*.py" = ["B008", "PLR0913"]
"app/tests/**" = ["PLR0915", "PLR0913"]
"customs_tests/**" = ["PLR0915", "PLR0913"]
"alembic/versions/*.py" = ["PLR0915"]
```

含义:FastAPI 路由里 `Depends()`/`Query()` 做默认值,B008 不适用,注入参数也常超过 8 个;测试是线性 arrange/act/assert,豁免语句数/参数个数(圈复杂度仍管);Alembic `upgrade()` 经常是长段 DDL,豁免 PLR0915。EasyCustoms 把测试目录改成自己的名字即可;没有 `customs_tests/` 的宿主删掉那一行。

跑棘轮(体积 + 重复 + 规模类 ruff,对照宿主自己的基线):

```bash
python -m tools.quality_gates.runner --repo <仓库根> --config <gates.json>
```

runner 以宿主 ruff 配置文件所在目录为 `cwd`(可用 `--ruff-cwd` 覆盖),并传入扫描根的绝对路径,这样从仓库根还是 `backend/` 启动结果一致。把 EasyFrame 的 `backend/tools/quality_gates/` 放进 `PYTHONPATH`(镜像里与 `enterprise_platform` 一样 COPY 到原路径即可)。`gates.json` 里写扫描根、阈值表、基线路径;缺省阈值与上表相同。

本仓:`make lint` = `ruff check`(忽略棘轮规则) + `ruff format --check` + runner + `platform_tests/test_quality_gate_*.py`;`make lint-fix` = RUF100 `--fix` + `ruff format`。

## 棘轮

`.code-smells-baseline.json` 按规则冻结存量。函数级规则(C901、PLR0915、PLR0912、PLR0911、PLR1702、PLR0913、PLR0917、function-sloc)键是 `文件::符号`;文件级规则(file-sloc、fixture-sloc、duplicate-window)仍是「文件 → 数值列表」。新符号超标 → 红;改名/删除的旧符号只是消失,不构成回归。同一文件里 A 变好、B 变差不能再用排序后的数值列表互相抵消。

重复按文件对的规范化重复行总数比较,不按连续区段条数;在中间改一行把一块拆成两块,只要总重复行没有增加,就不是回归。规范化按 token 进行(字符串字面量原样保留),整窗只有 import/from、装饰器或字段注解的窗口不计入。

新违规或同一键数值变差 → 红;变好 → 绿,并提示 `tighten with --update`。`--update` 只允许收缩,拒绝把基线写大。从旧的「按文件列数值」基线迁到符号键时,对函数级规则先按文件聚合再判断是否膨胀,确认没有变差后 `--update` 会写成 `文件::符号`。

**抬高阈值或加 `# noqa` 必须写明理由。** 欠账靠拆函数和删重复还,不靠放宽门禁。
