"""Athenai + Claude Agent SDK 兼容性 PoC — Phase 0 可行性验证。

验证 4 项致命风险:
  A. Athenai 是否完整透传 Anthropic tool_use / tool_result block (test_1)
  B. Claude Agent SDK Python 包是否能走 Athenai 完成真实任务   (test_3)
  C. agent 长链路 (8+ 轮) rate limit / 稳定性                (test_2)
  D. ANTHROPIC_AUTH_TOKEN vs ANTHROPIC_API_KEY 兼容性        (test_3, env 同时设)

跑法:
  cd bot/
  source .venv/bin/activate
  pip install claude-agent-sdk
  ATHENAI_API_KEY=sk-... python scripts/poc_agent_athenai.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx

BASE = "https://athenai.mihoyo.com"
MODEL = "claude-sonnet-4-6"
API_KEY = os.environ.get("ATHENAI_API_KEY") or ""

if not API_KEY:
    print("ERROR: 设置 ATHENAI_API_KEY 环境变量", file=sys.stderr)
    sys.exit(2)


# ─────────────────────────────────────────────────────────────────
# Test 1 — tool_use / tool_result 完整透传 (致命风险 A)
# ─────────────────────────────────────────────────────────────────
def test_1_tool_use_passthrough() -> bool:
    print("\n=== Test 1: tool_use / tool_result 透传 ===")
    tools = [{
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }]
    headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01"}

    # Round 1 — 期望 stop_reason=tool_use
    r1 = httpx.post(
        f"{BASE}/v1/messages",
        headers=headers,
        json={
            "model": MODEL,
            "max_tokens": 1024,
            "tools": tools,
            "messages": [{"role": "user", "content": "What's the weather in Tokyo right now? Use the tool."}],
        },
        timeout=60,
    )
    if r1.status_code != 200:
        print(f"  ❌ HTTP {r1.status_code}: {r1.text[:300]}")
        return False
    j1 = r1.json()
    print(f"  Round 1 stop_reason={j1.get('stop_reason')}, content blocks={[b.get('type') for b in j1.get('content', [])]}")
    if j1.get("stop_reason") != "tool_use":
        print(f"  ❌ 期望 stop_reason=tool_use, 实际={j1.get('stop_reason')}")
        print(f"  full response: {json.dumps(j1, ensure_ascii=False)[:500]}")
        return False
    tool_use = next((b for b in j1["content"] if b.get("type") == "tool_use"), None)
    if not tool_use:
        print(f"  ❌ content 里没有 tool_use 块")
        return False
    print(f"  ✅ Round 1 拿到 tool_use: name={tool_use['name']}, input={tool_use['input']}, id={tool_use['id'][:20]}...")

    # Round 2 — 回 tool_result, 期望拿到最终文本
    r2 = httpx.post(
        f"{BASE}/v1/messages",
        headers=headers,
        json={
            "model": MODEL,
            "max_tokens": 1024,
            "tools": tools,
            "messages": [
                {"role": "user", "content": "What's the weather in Tokyo right now? Use the tool."},
                {"role": "assistant", "content": j1["content"]},
                {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use["id"],
                    "content": "22°C, sunny, light breeze",
                }]},
            ],
        },
        timeout=60,
    )
    if r2.status_code != 200:
        print(f"  ❌ Round 2 HTTP {r2.status_code}: {r2.text[:300]}")
        return False
    j2 = r2.json()
    text_block = next((b for b in j2.get("content", []) if b.get("type") == "text"), None)
    if not text_block or "22" not in text_block.get("text", ""):
        print(f"  ❌ Round 2 没拿到包含 tool_result 内容的回复")
        print(f"  full: {json.dumps(j2, ensure_ascii=False)[:500]}")
        return False
    print(f"  ✅ Round 2 final text: {text_block['text'][:120]}...")
    return True


# ─────────────────────────────────────────────────────────────────
# Test 2 — streaming + 长链多轮 tool 调度稳定性 (风险 C)
# ─────────────────────────────────────────────────────────────────
def test_2_streaming_long_chain() -> bool:
    print("\n=== Test 2: streaming + 长链 (8+ 轮 tool 调度) ===")
    tools = [{
        "name": "increment",
        "description": "Add 1 to a number and return the result.",
        "input_schema": {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    }]
    headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01"}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Starting from 0, call the increment tool 8 times in a row, each time using the previous result as input. After all 8 calls, tell me the final number."},
    ]

    rounds = 0
    started = time.time()
    while rounds < 12:
        rounds += 1
        # 用非 stream 简化 long-chain 验证 (stream 单独在 round 1 验一下足够)
        r = httpx.post(
            f"{BASE}/v1/messages",
            headers=headers,
            json={"model": MODEL, "max_tokens": 1024, "tools": tools, "messages": messages},
            timeout=60,
        )
        if r.status_code == 429:
            print(f"  ⚠️ 第 {rounds} 轮触发 429, 这是 rate limit 风险信号")
            print(f"     headers: {dict(r.headers)}")
            return False
        if r.status_code != 200:
            print(f"  ❌ Round {rounds} HTTP {r.status_code}: {r.text[:300]}")
            return False
        j = r.json()
        stop = j.get("stop_reason")
        print(f"  Round {rounds}: stop={stop}, dt={time.time()-started:.1f}s")
        messages.append({"role": "assistant", "content": j["content"]})
        if stop == "end_turn":
            text = next((b["text"] for b in j["content"] if b.get("type") == "text"), "")
            print(f"  ✅ 长链结束, 共 {rounds} 轮, 最终文本: {text[:120]}...")
            break
        if stop == "tool_use":
            tu = next(b for b in j["content"] if b.get("type") == "tool_use")
            n = tu["input"].get("n", 0)
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": str(n + 1),
            }]})
        else:
            print(f"  ❌ 未知 stop_reason={stop}")
            return False
    else:
        print(f"  ❌ 12 轮内未结束, 链路异常")
        return False

    # 单独验一下 stream — 拉一次 stream=true
    print("  ─ streaming 子测试 ─")
    deltas = 0
    msg_stop = False
    with httpx.stream(
        "POST",
        f"{BASE}/v1/messages",
        headers=headers,
        json={"model": MODEL, "max_tokens": 256, "stream": True,
              "messages": [{"role": "user", "content": "Count 1 to 5."}]},
        timeout=60,
    ) as resp:
        if resp.status_code != 200:
            print(f"  ❌ stream HTTP {resp.status_code}")
            return False
        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            t = evt.get("type")
            if t == "content_block_delta":
                deltas += 1
            elif t == "message_stop":
                msg_stop = True
    if not (deltas > 0 and msg_stop):
        print(f"  ❌ stream 异常: deltas={deltas}, msg_stop={msg_stop}")
        return False
    print(f"  ✅ stream 完整: deltas={deltas}, message_stop=true")
    return True


# ─────────────────────────────────────────────────────────────────
# Test 3 — Claude Agent SDK 实际接入 (风险 B + D)
# ─────────────────────────────────────────────────────────────────
async def test_3_claude_agent_sdk() -> bool:
    print("\n=== Test 3: Claude Agent SDK + Athenai + 真实文件写入 ===")
    try:
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    except ImportError:
        print("  ❌ claude-agent-sdk 未安装, 跳过. 运行: pip install claude-agent-sdk")
        return False

    workdir = "/tmp/poc-agent-workdir"
    os.makedirs(workdir, exist_ok=True)
    target = os.path.join(workdir, "hello.txt")
    if os.path.exists(target):
        os.remove(target)

    # env 同时设两个名字, 看 SDK 实际生效哪个 (风险 D 验证)
    env_overrides = {
        "ANTHROPIC_BASE_URL": BASE,
        "ANTHROPIC_AUTH_TOKEN": API_KEY,
        "ANTHROPIC_API_KEY": API_KEY,
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        "ANTHROPIC_MODEL": MODEL,
    }
    for k, v in env_overrides.items():
        os.environ[k] = v

    options = ClaudeAgentOptions(
        cwd=workdir,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Write", "Bash"],
        max_turns=10,
    )

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                "Create a file called hello.txt in the current directory with the content 'hello athenai poc'. "
                "Use the Write tool. After creating it, confirm it exists."
            )
            async for msg in client.receive_response():
                # 打印简化摘要, 不刷屏
                t = type(msg).__name__
                preview = str(msg)[:160].replace("\n", " ")
                print(f"  [{t}] {preview}")
    except Exception as e:
        print(f"  ❌ SDK 调用失败: {type(e).__name__}: {e}")
        return False

    if not os.path.exists(target):
        print(f"  ❌ agent 没创建 {target}")
        return False
    content = open(target).read()
    print(f"  ✅ 文件已创建: {target}")
    print(f"     内容: {content!r}")
    return True


# ─────────────────────────────────────────────────────────────────
def main() -> int:
    results = {}
    results["test_1_tool_use"] = test_1_tool_use_passthrough()
    results["test_2_long_chain"] = test_2_streaming_long_chain()
    results["test_3_sdk"] = asyncio.run(test_3_claude_agent_sdk())

    print("\n" + "=" * 60)
    print("结果汇总:")
    for k, v in results.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print("=" * 60)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
