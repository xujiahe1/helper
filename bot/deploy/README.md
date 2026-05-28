# Helper bot deployment

> 服务器: `10.234.81.212`(Ubuntu 22.04 / 2C 3.6G 40G,**不可升配**)
> 一切线上动作都在准备 deploy 时做,本地开发不要碰。

## 一次性 bootstrap

```bash
# 1) 系统包
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv git nginx

# 2) helper 用户 + 数据目录
sudo useradd -r -m -d /opt/helper -s /bin/bash helper
sudo mkdir -p /var/lib/helper /var/log/helper /etc/helper
sudo chown -R helper:helper /var/lib/helper /var/log/helper
sudo chown -R root:helper /etc/helper && sudo chmod 750 /etc/helper

# 3) 凭据(只在服务器,不入 git)
sudo cp wave.env.template /etc/helper/wave.env
sudo cp helper.env.template /etc/helper/helper.env
sudo chmod 600 /etc/helper/*.env
sudo $EDITOR /etc/helper/wave.env       # 填 APP_SECRET / AES_KEY / SIGN_TOKEN
sudo $EDITOR /etc/helper/helper.env     # 填 ATHENAI_API_KEY / HELPER_ADMIN_SK

# 4) 拉代码 + venv + 装包
sudo -u helper git clone https://github.com/xujiahe1/helper.git /opt/helper/bot
cd /opt/helper/bot
sudo -u helper python3.11 -m venv .venv
sudo -u helper .venv/bin/pip install -e .

# 5) 初始化 sqlite + git spec repo
sudo -u helper -E .venv/bin/helper init

# 6) systemd unit
sudo cp deploy/helper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now helper

# 7)(可选)nginx 反代 admin 走 https
sudo cp deploy/nginx.conf /etc/nginx/sites-available/helper
sudo ln -sf ../sites-available/helper /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 8) Wave 后台配置回调 URL
#    URL = mhynetcn://10.234.81.212:8009/callback
#    Wave 会下发一条 challenge,bot 解密后回显,看到「URL 验证通过」即接入完成
```

## 运维命令

```bash
# 看日志
journalctl -u helper -f
tail -f /var/log/helper/helper.log

# 重启 / 停服
sudo systemctl restart helper
sudo systemctl stop helper

# Smoke test
curl http://127.0.0.1:8009/healthz
curl -H "X-Helper-Admin-Key: $(sudo grep HELPER_ADMIN_SK /etc/helper/helper.env | cut -d= -f2)" \
     http://127.0.0.1:8009/admin/healthz

# 升级
cd /opt/helper/bot
sudo -u helper git pull
sudo -u helper .venv/bin/pip install -e .
sudo systemctl restart helper
```

## 凭据轮换

```bash
sudo $EDITOR /etc/helper/wave.env
sudo systemctl restart helper
```

## 内存看护

服务器只 3.6G,helper.service 已在 unit 里限了 `MemoryMax=900M`。
若 OOM:

```bash
journalctl -u helper -p err --since "10 min ago"
free -h
```

如确实超,先看是不是 Athenai 同时跑了多次 ask;`asyncio.to_thread` 默认线程池 32,
M2 起若回调高峰可在 server.py 里改 `concurrent.futures.ThreadPoolExecutor(max_workers=4)` 的全局池。

## 不做

- Docker / k8s — baseline 500M+ 占不起
- Postgres — sqlite 够用
- 本地跑模型 — 全走 Athenai
- 多 worker 进程 — `--workers 1`,asyncio
