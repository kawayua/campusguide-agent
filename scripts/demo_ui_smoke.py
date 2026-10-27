"""W8 页面级 smoke：CDP 驱动真实 Gradio，跑 5 场景 + 拒答并计时。

前置：Qdrant 已启动、真实 app 已运行（建议 ``W8_DEMO_PROFILE=ultraspeed``
且角色模型均切到快速版）。脚本不启动 app、不注入 fake 数据，只读取页面
上的 outcome / checklist / 引用，验证真实 UI 链路。

Windows 默认使用 Edge；其它平台用 ``--edge`` 指定 Chromium 可执行文件。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import websockets

ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "data" / "eval" / "w8-ui-smoke.json"
OUT_MD = ROOT / "data" / "eval" / "w8-ui-smoke.md"
DEFAULT_EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
LABEL_TO_OUTCOME = {
    "已核实回答": "ANSWERED",
    "部分回答": "PARTIAL_ANSWER",
    "需要补充信息": "CLARIFY",
    "未作答": "ABSTAIN",
    "未作答 · 无可引用依据": "NO_EVIDENCE",
    "未作答 · 未通过核对": "UNVERIFIABLE",
    "未作答 · 仅废止版本": "ONLY_REPEALED",
    "未作答 · 版本不符": "VERSION_MISMATCH",
    "未作答 · 版本冲突": "VERSION_CONFLICT",
    "历史版本暂不支持": "HISTORICAL_NOT_SUPPORTED",
    "未作答 · 涉及个人信息": "PRIVACY_REQUEST",
    "超出范围": "OUT_OF_SCOPE",
    "请输入具体问题": "INVALID_REQUEST",
    "生成服务暂不可用": "GENERATION_FAILED",
    "未生成可核对结论": "NO_VALID_CLAIM",
    "索引不可用": "INDEX_UNAVAILABLE",
    "检索工具不可用": "TOOL_UNAVAILABLE",
}

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "policy",
        "name": "政策问答：国奖条件",
        "messages": ["申请国家奖学金需要满足哪些条件？"],
        "allowed": ["ANSWERED"],
        "min_citations": 1,
    },
    {
        "id": "closed",
        "name": "流程 closed：国奖怎么申请",
        "messages": ["国家奖学金怎么申请"],
        "allowed": ["ANSWERED", "PARTIAL_ANSWER"],
        "closed": True,
        "min_checklist": 1,
    },
    {
        "id": "certificate",
        "name": "多轮澄清：在读证明",
        "messages": ["我要开在读证明"],
        "clarify": ["本科生", "在校生", "升学"],
        "allowed": ["ANSWERED", "PARTIAL_ANSWER"],
        "min_clarify": 1,
        "min_checklist": 1,
    },
    {
        "id": "mixed",
        "name": "混合问题：怎么申请 + 金额",
        "messages": ["国家奖学金怎么申请，金额是多少"],
        "allowed": ["ANSWERED", "PARTIAL_ANSWER"],
        "min_checklist": 1,
        "min_citations": 1,
    },
    {
        "id": "privacy",
        "name": "隐私拒答：绩点与排名",
        "messages": ["查一下我的绩点和排名"],
        "allowed": ["PRIVACY_REQUEST"],
        "refusal": True,
    },
    {
        "id": "out_of_scope",
        "name": "校外拒答：隔壁学校奖学金",
        "messages": ["隔壁学校的奖学金政策是什么？"],
        "allowed": ["OUT_OF_SCOPE"],
        "refusal": True,
    },
]
CORE_IDS = {"policy", "closed", "certificate", "mixed", "privacy"}


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
    return float(ordered[index])


class CDP:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._seq = 0

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._seq += 1
        await self._ws.send(json.dumps({"id": self._seq, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("id") != self._seq:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method}: {message['error']}")
            return message.get("result", {})

    async def eval(self, expression: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("value")

    async def screenshot(self, path: Path) -> None:
        result = await self.call(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result["data"]))


STATE_JS = """(() => {
  const statusEl = document.getElementById('w8-status');
  const status = statusEl && getComputedStyle(statusEl).display !== 'none'
    ? (statusEl.innerText || '').trim() : '';
  const visible = el => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const options = [...document.querySelectorAll('[id^="w8-opt-"]')]
    .filter(visible)
    .map(el => (el.innerText || '').trim())
    .filter(Boolean);
  const badges = [...document.querySelectorAll('#w8-conversation-head .w8-badge')]
    .map(el => (el.innerText || '').trim())
    .filter(Boolean);
  const debugOutcome = document.querySelector('#w8-debug .w8-debug-grid b');
  const profile = document.querySelector('#w8-conversation-head .w8-profile-badge');
  const lastBot = [...document.querySelectorAll('#w8-chatbot .message.bot')].pop();
  return {
    bots: document.querySelectorAll('#w8-chatbot .message.bot').length,
    users: document.querySelectorAll('#w8-chatbot .message.user').length,
    status,
    options,
    badges,
    debugOutcome: (debugOutcome?.innerText || '').trim(),
    profile: (profile?.innerText || '').trim(),
    title: (document.querySelector('#w8-conversation-head h2')?.innerText || '').trim(),
    checklist: document.querySelectorAll('#w8-checklist .w8-step').length,
    citations: document.querySelectorAll('#w8-citations .w8-cite-card').length,
    sideVisible: visible(document.getElementById('w8-side')),
    sideHeight: Math.round(document.getElementById('w8-side')?.getBoundingClientRect().height || 0),
    answerHead: (lastBot?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 220),
  };
})()"""


def outcome_of(state: dict[str, Any]) -> str:
    if state.get("debugOutcome"):
        return str(state["debugOutcome"])
    for badge in state.get("badges") or []:
        if badge in LABEL_TO_OUTCOME:
            return LABEL_TO_OUTCOME[badge]
    return ""


async def wait_ready(cdp: CDP, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = await cdp.eval(
            "document.readyState==='complete' && !!document.getElementById('w8-root')"
            " && !!document.querySelector('#w8-input textarea')"
        )
        if ready:
            await asyncio.sleep(2.0)
            return
        await asyncio.sleep(0.4)
    raise RuntimeError("页面未在 60s 内就绪")


async def click(cdp: CDP, selector: str) -> bool:
    return bool(
        await cdp.eval(
            f"""(() => {{
              const el=document.querySelector({json.dumps(selector)});
              if(!el) return false;
              el.click();
              return true;
            }})()"""
        )
    )


async def fill_input(cdp: CDP, text: str) -> None:
    ok = await cdp.eval(
        f"""(() => {{
          const ta=document.querySelector('#w8-input textarea');
          if(!ta) return false;
          const setter=Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype,'value'
          ).set;
          setter.call(ta,{json.dumps(text)});
          ta.dispatchEvent(new Event('input',{{bubbles:true}}));
          ta.dispatchEvent(new Event('change',{{bubbles:true}}));
          return true;
        }})()"""
    )
    if not ok:
        raise RuntimeError("输入框不存在")


async def wait_turn(
    cdp: CDP, before_bots: int, timeout: float
) -> tuple[dict[str, Any], list[str], str]:
    deadline = time.monotonic() + timeout
    progress: list[str] = []
    last = await cdp.eval(STATE_JS)
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        state = await cdp.eval(STATE_JS)
        last = state
        status = str(state.get("status") or "")
        if status and status not in progress:
            progress.append(status)
        if any(
            marker in status
            for marker in ("本轮超过", "服务暂时不可用", "模型配置缺失", "请求超时")
        ):
            return state, progress, "error"
        if state["bots"] > before_bots and not status:
            await asyncio.sleep(0.55)
            return await cdp.eval(STATE_JS), progress, ""
    return last, progress, "timeout"


async def click_option(cdp: CDP, label: str) -> bool:
    return bool(
        await cdp.eval(
            f"""(() => {{
              const options=[...document.querySelectorAll('[id^="w8-opt-"]')];
              const target=options.find(el => {{
                const rect=el.getBoundingClientRect();
                return rect.width>0 && (el.innerText || '').includes({json.dumps(label)});
              }});
              if(!target) return false;
              target.click();
              return true;
            }})()"""
        )
    )


async def click_tab(cdp: CDP, label: str) -> bool:
    return bool(
        await cdp.eval(
            f"""(() => {{
              const tabs=[...document.querySelectorAll('#w8-tabs [role="tab"]')];
              const target=tabs.find(el => (el.innerText || '').trim() === {json.dumps(label)});
              if(!target) return false;
              target.click();
              return true;
            }})()"""
        )
    )


async def read_full_state(cdp: CDP) -> dict[str, Any]:
    """引用/调试内容可能惰性挂载；必要时切 tab 读取后回到清单。"""
    state = await cdp.eval(STATE_JS)
    needs_citation_tab = state.get("sideVisible") and not state.get("citations")
    if needs_citation_tab and await click_tab(cdp, "引用"):
        count = 0
        for _ in range(8):
            await asyncio.sleep(0.15)
            count = int(
                await cdp.eval("document.querySelectorAll('#w8-citations .w8-cite-card').length")
                or 0
            )
            if count:
                break
        state["citations"] = count
        await click_tab(cdp, "办理清单")
        await asyncio.sleep(0.2)
    return state


async def read_debug_metadata(cdp: CDP) -> dict[str, str]:
    if not await click_tab(cdp, "调试"):
        return {}
    for _ in range(8):
        await asyncio.sleep(0.15)
        ready = await cdp.eval("document.querySelectorAll('#w8-debug dt').length > 0")
        if ready:
            break
    data = await cdp.eval(
        """(() => {
          const find = label => {
            const dt=[...document.querySelectorAll('#w8-debug dt')]
              .find(el => (el.textContent || '').trim() === label);
            return (dt?.nextElementSibling?.textContent || '').trim();
          };
          return {model_profile: find('model_profile'), models: find('models')};
        })()"""
    )
    await click_tab(cdp, "办理清单")
    await asyncio.sleep(0.2)
    return data or {}


def check_scenario(result: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    turns = result["turns"]
    final = turns[-1] if turns else {}
    outcome = str(final.get("outcome") or "")
    if outcome not in spec["allowed"]:
        failures.append(f"outcome {outcome!r} 不在 {spec['allowed']}")
    if spec.get("closed") and not final.get("closed"):
        failures.append("未显示批次已截止")
    if final.get("checklist", 0) < int(spec.get("min_checklist", 0)):
        failures.append(f"checklist {final.get('checklist')} 少于 {spec['min_checklist']}")
    if final.get("citations", 0) < int(spec.get("min_citations", 0)):
        failures.append(f"citations {final.get('citations')} 少于 {spec['min_citations']}")
    clarify_count = sum(1 for turn in turns if turn.get("outcome") == "CLARIFY")
    if clarify_count < int(spec.get("min_clarify", 0)):
        failures.append(f"clarify {clarify_count} 少于 {spec['min_clarify']}")
    if any(turn.get("error") for turn in turns):
        failures.append("至少一轮出现错误状态")
    return failures


async def run_scenario(cdp: CDP, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    await click(cdp, "#w8-new-chat")
    await asyncio.sleep(0.9)
    state = await cdp.eval(STATE_JS)
    if state["bots"] != 0:
        raise RuntimeError(f"新会话未清空 chatbot：bots={state['bots']}")
    turns: list[dict[str, Any]] = []
    messages = list(spec["messages"])
    clarify = list(spec.get("clarify") or [])
    for index, message in enumerate(messages):
        await fill_input(cdp, message)
        await asyncio.sleep(0.3)
        before = int((await cdp.eval(STATE_JS))["bots"])
        started = time.perf_counter()
        if not await click(cdp, "#w8-send"):
            raise RuntimeError("发送按钮不可点击")
        state, progress, error = await wait_turn(cdp, before, timeout)
        duration_ms = int((time.perf_counter() - started) * 1000)
        turns.append(
            {
                "message": message,
                "via": "input",
                "outcome": outcome_of(state),
                "badges": state.get("badges") or [],
                "closed": "批次已截止" in (state.get("badges") or []),
                "checklist": state.get("checklist", 0),
                "citations": state.get("citations", 0),
                "duration_ms": duration_ms,
                "progress": progress,
                "error": error,
                "answer_head": state.get("answerHead", ""),
            }
        )
        if index == 0 and clarify:
            for label in clarify:
                current = await cdp.eval(STATE_JS)
                available = current.get("options") or []
                if label not in available:
                    final_outcome = outcome_of(current)
                    if final_outcome in spec["allowed"] and final_outcome != "CLARIFY":
                        break
                    turns.append(
                        {
                            "message": label,
                            "via": "clarify",
                            "outcome": "",
                            "duration_ms": 0,
                            "error": f"未找到澄清选项 {label}；可见={available}",
                        }
                    )
                    return {
                        "id": spec["id"],
                        "name": spec["name"],
                        "turns": turns,
                        "final_outcome": turns[-2].get("outcome", ""),
                        "failures": [f"clarify 选项缺失：{label}"],
                        "pass": False,
                    }
                before = int((await cdp.eval(STATE_JS))["bots"])
                started = time.perf_counter()
                if not await click_option(cdp, label):
                    raise RuntimeError(f"点击澄清选项失败：{label}")
                state, progress, error = await wait_turn(cdp, before, timeout)
                duration_ms = int((time.perf_counter() - started) * 1000)
                turns.append(
                    {
                        "message": label,
                        "via": "clarify",
                        "outcome": outcome_of(state),
                        "badges": state.get("badges") or [],
                        "closed": "批次已截止" in (state.get("badges") or []),
                        "checklist": state.get("checklist", 0),
                        "citations": state.get("citations", 0),
                        "duration_ms": duration_ms,
                        "progress": progress,
                        "error": error,
                        "answer_head": state.get("answerHead", ""),
                    }
                )
    full = await read_full_state(cdp)
    if turns:
        turns[-1].update(
            {
                "outcome": outcome_of(full),
                "badges": full.get("badges") or [],
                "closed": "批次已截止" in (full.get("badges") or []),
                "checklist": full.get("checklist", 0),
                "citations": full.get("citations", 0),
                "answer_head": full.get("answerHead", ""),
            }
        )
    final = turns[-1] if turns else {}
    result = {
        "id": spec["id"],
        "name": spec["name"],
        "turns": turns,
        "final_outcome": final.get("outcome", ""),
        "final_closed": bool(final.get("closed")),
        "final_checklist": final.get("checklist", 0),
        "final_citations": final.get("citations", 0),
        "total_ms": sum(int(turn.get("duration_ms", 0)) for turn in turns),
        "refusal": bool(spec.get("refusal")),
    }
    result["failures"] = check_scenario(result, spec)
    result["pass"] = not result["failures"]
    return result


async def drive(args: argparse.Namespace) -> dict[str, Any]:
    edge = args.edge or os.getenv("EDGE_PATH", "") or DEFAULT_EDGE
    if not Path(edge).exists():
        raise SystemExit(f"找不到 Chromium/Edge：{edge}；请用 --edge 指定")
    try:
        requests.get(args.url, timeout=5)
    except Exception as exc:
        raise SystemExit(f"无法访问 {args.url}：{exc}") from exc

    profile_dir = tempfile.TemporaryDirectory(prefix="w8-ui-smoke-")
    port = args.cdp_port
    proc = subprocess.Popen(
        [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir.name}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={args.width},{args.height}",
            args.url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ws_url = ""
    try:
        for _ in range(80):
            try:
                pages = requests.get(f"http://127.0.0.1:{port}/json", timeout=1).json()
            except Exception:
                pages = []
            for page in pages:
                if page.get("type") == "page" and args.url.split("//")[-1] in page.get("url", ""):
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            if ws_url:
                break
            await asyncio.sleep(0.25)
        if not ws_url:
            raise RuntimeError("无法连接浏览器 CDP")

        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
            cdp = CDP(ws)
            await cdp.call("Page.enable")
            await cdp.call("Runtime.enable")
            await cdp.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": args.width,
                    "height": args.height,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )
            await cdp.call("Page.navigate", {"url": args.url})
            await wait_ready(cdp)
            initial = await cdp.eval(STATE_JS)
            profile_badge = str(initial.get("profile") or "")
            results: list[dict[str, Any]] = []
            debug_metadata: dict[str, str] = {}
            for spec in SCENARIOS:
                print(f"[{spec['id']}] {spec['name']}", flush=True)
                try:
                    result = await run_scenario(cdp, spec, args.turn_timeout)
                except Exception as exc:
                    result = {
                        "id": spec["id"],
                        "name": spec["name"],
                        "turns": [],
                        "final_outcome": "",
                        "failures": [f"{type(exc).__name__}: {exc}"],
                        "pass": False,
                    }
                if not debug_metadata:
                    debug_metadata = await read_debug_metadata(cdp)
                results.append(result)
                if args.screenshots:
                    await cdp.screenshot(Path(args.screenshots) / f"{spec['id']}.png")
                for turn in result["turns"]:
                    print(
                        f"  {turn.get('message')} [{turn.get('via')}] -> "
                        f"{turn.get('outcome') or 'ERROR'} {turn.get('duration_ms')}ms",
                        flush=True,
                    )
                print(
                    f"  pass={result['pass']} failures={result.get('failures')}",
                    flush=True,
                )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        profile_dir.cleanup()

    durations = [
        float(turn.get("duration_ms", 0))
        for result in results
        for turn in result["turns"]
        if turn.get("duration_ms")
    ]
    core = [result for result in results if result["id"] in CORE_IDS]
    refusals = [result for result in results if result.get("refusal")]
    summary = {
        "total": len(results),
        "passed": sum(1 for result in results if result["pass"]),
        "failed": [result["id"] for result in results if not result["pass"]],
        "core_total": len(core),
        "core_passed": sum(1 for result in core if result["pass"]),
        "refusal_total": len(refusals),
        "refusal_passed": sum(1 for result in refusals if result["pass"]),
        "turns": len(durations),
        "p50_turn_ms": int(statistics.median(durations)) if durations else 0,
        "p95_turn_ms": int(p95(durations)),
        "target_p50_ms": 15000,
        "target_p95_ms": 45000,
    }
    summary["target_met"] = (
        summary["p50_turn_ms"] <= summary["target_p50_ms"]
        and summary["p95_turn_ms"] <= summary["target_p95_ms"]
    )
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "url": args.url,
        "viewport": [args.width, args.height],
        "profile_badge": profile_badge,
        "expected_profile": args.expect_profile,
        "profile_ok": (
            not args.expect_profile or args.expect_profile.lower() in profile_badge.lower()
        ),
        "actual_model_profile": debug_metadata.get("model_profile", ""),
        "actual_models": debug_metadata.get("models", ""),
        "request_timeout_seconds": args.request_timeout,
        "scenarios": results,
        "summary": summary,
    }


def write_report(payload: dict[str, Any]) -> None:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = payload["summary"]
    lines = [
        "# W8 真实 UI 页面级 smoke",
        "",
        f"- 时间：{payload['generated_at']}",
        f"- URL：{payload['url']}（{payload['viewport'][0]}×{payload['viewport'][1]}）",
        f"- 页面 profile：`{payload['profile_badge'] or '—'}`",
        f"- 实际模型：profile=`{payload.get('actual_model_profile') or '—'}`；"
        f"models=`{payload.get('actual_models') or '—'}`",
        f"- 请求上限：{payload.get('request_timeout_seconds', 120):.0f}s（默认 120s；超时 fail-closed）",
        f"- 核心场景：**{summary['core_passed']}/{summary['core_total']} PASS**",
        f"- 拒答：**{summary['refusal_passed']}/{summary['refusal_total']} PASS**",
        f"- 全部场景：**{summary['passed']}/{summary['total']} PASS**",
        f"- 单轮 p50：{summary['p50_turn_ms']}ms；p95：{summary['p95_turn_ms']}ms；"
        f"目标 p50≤{summary['target_p50_ms']}ms / p95≤{summary['target_p95_ms']}ms："
        f"{'达标' if summary['target_met'] else '未达标'}",
        "",
        "| 场景 | 轮次 | 最终 outcome | 耗时 | checklist | 引用 | clarify | 结果 |",
        "|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for result in payload["scenarios"]:
        clarify_count = sum(1 for turn in result["turns"] if turn.get("outcome") == "CLARIFY")
        lines.append(
            f"| {result['name']} | {len(result['turns'])} | "
            f"{result.get('final_outcome') or '—'} | {result.get('total_ms', 0)}ms | "
            f"{result.get('final_checklist', 0)} | {result.get('final_citations', 0)} | "
            f"{clarify_count} | {'PASS' if result['pass'] else 'FAIL'} |"
        )
    lines.extend(["", "## 失败原因", ""])
    failures = [result for result in payload["scenarios"] if result.get("failures")]
    if not payload.get("profile_ok", True):
        lines.append(
            f"- profile 不匹配：页面 `{payload['profile_badge']}`，"
            f"期望含 `{payload['expected_profile']}`"
        )
    if failures:
        for result in failures:
            lines.append(f"- `{result['id']}`：{'；'.join(result['failures'])}")
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 边界",
            "",
            "- 本报告只验证真实 Gradio 页面和固定场景；不替代 W6 20 题 / W7 冻结集报告。",
            "- 页面展示档与真实模型分开记录；不把本页延迟写成 ultraspeed 指标，也不混入 W6 指标。",
            "- 应用默认单轮上限 120s；页面级 smoke 若为容纳 vPro 长尾调大上限，会在上方单独记录。",
            "- 页面级 smoke 不检查答案支持度，人工 support 标注仍以既有评测报告为准。",
        ]
    )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7860/")
    parser.add_argument("--edge", default="")
    parser.add_argument("--cdp-port", type=int, default=9338)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--turn-timeout", type=float, default=135.0)
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=float(os.getenv("W8_REQUEST_TIMEOUT_SECONDS", "120")),
        help="仅写入报告，便于说明应用单轮上限；脚本等待仍由 --turn-timeout 控制",
    )
    parser.add_argument("--expect-profile", default="ultraspeed")
    parser.add_argument("--screenshots", default="")
    args = parser.parse_args()
    payload = asyncio.run(drive(args))
    write_report(payload)
    print(f"RESULTS {payload['summary']['passed']}/{payload['summary']['total']} PASS")
    print(f"JSON {OUT_JSON}")
    print(f"MD   {OUT_MD}")


if __name__ == "__main__":
    main()
