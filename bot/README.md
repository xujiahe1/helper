# bot/

Helper Python 实现根。设计文档在仓库根的 [`docs/`](../docs/)。

## 本地开发

```bash
cd bot/
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env
# 编辑 .env 填入 ATHENAI_API_KEY

helper hello   # 烟测试
```

## 目录

模块拆分见 [`docs/architecture.md`](../docs/architecture.md) §4 内部模块。

`var/`(本地数据)放在仓库根,不在 `bot/` 内。M10 Agent Surface 实装时,agent 工作目录走 `/var/lib/helper/agent-workdir/`,详见 `docs/runtime.md` §4。

## 部署

部署目标 `10.234.81.212`,流程见 [`bot/deploy/README.md`](deploy/README.md)。
