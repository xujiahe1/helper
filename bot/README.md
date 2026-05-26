# bot/

Helper Python 实现根。设计文档在仓库根的 [`docs/`](../docs/)。

## 本地开发(Month 1 阶段)

```bash
cd bot/
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env
# 编辑 .env 填入 ATHENAI_API_KEY

helper hello   # 烟测试
```

## 目录(随 Month 1 实施填充)

```
bot/
  helper/
    __init__.py
    cli.py
    core/         (待建)
    ingest/       (待建)
    ontology/     (待建)
    elicit/       (待建,追问 engine)
    conflict/     (待建)
    store/        (待建)
    compile/      (待建)
    runtime/      (待建)
    router/       (待建,model_router)
    im/           (Month 2 才接 Wave)
    web/          (待建,Backend Web)
  pyproject.toml
  .env.example
```

`extensions/` 目录(自迭代外挂层)和 `var/`(本地数据)放在仓库根,不在 `bot/` 内。

## 部署目标

`10.234.81.212` — 但**只在 Month 2 末才部署**。开发阶段全程本地。
