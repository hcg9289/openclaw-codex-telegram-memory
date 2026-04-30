#!/usr/bin/env python3
import json


MEMORY_RULES = """Current facts are authoritative.
Cognee memory is historical context, not current truth.
If current facts conflict with cognee memory, trust current facts.
Never output secrets, private keys, tokens, passwords, or raw .env contents.
Destructive deletion, private key/token writes, and full .env overwrite are prohibited."""


def render_context(memory_context):
    return json.dumps(memory_context, ensure_ascii=False, indent=2)[:18000]


def planner_prompt(user_text, memory_context):
    return f"""你是 Codex 主代理 Orchestrator Planner。你的任務不是直接完成工作，而是做 harness engineering：拆分任務、設計子代理 phases、限定上下文與輸出格式。

{MEMORY_RULES}

[MEMORY_CONTEXT]
{render_context(memory_context)}
[/MEMORY_CONTEXT]

[USER_TASK]
{user_text}
[/USER_TASK]

請只輸出一個 JSON object，不要 markdown，不要解釋。schema:
{{
  "job_type": "quick_answer|multi_phase",
  "summary": "short summary",
  "phases": [
    {{
      "id": "explorer",
      "role": "explorer|auditor|runner|verifier|reporter",
      "objective": "what this worker must do",
      "depends_on": [],
      "timeout_seconds": 900,
      "prompt": "complete worker instruction"
    }}
  ]
}}

規則：
- 短問題可用一個 reporter phase。
- 涉及代碼、交易、回測、部署、debug 的任務必須拆成 explorer/auditor或runner/verifier/reporter。
- 每個 phase prompt 必須要求輸出 evidence，例如 file path、line、command、result file。
- runner 可以跑命令，但不得刪除資料、不得覆寫 .env、不得輸出 secrets。
"""


def worker_prompt(role, objective, user_text, memory_context, previous_results, planner_prompt_text=""):
    return f"""你是 Codex 子代理 worker，角色：{role}

{MEMORY_RULES}

[ROLE_OBJECTIVE]
{objective}
[/ROLE_OBJECTIVE]

[ORIGINAL_USER_TASK]
{user_text}
[/ORIGINAL_USER_TASK]

[MEMORY_CONTEXT]
{render_context(memory_context)}
[/MEMORY_CONTEXT]

[PREVIOUS_PHASE_RESULTS]
{json.dumps(previous_results, ensure_ascii=False, indent=2)[:18000]}
[/PREVIOUS_PHASE_RESULTS]

[PLANNER_WORKER_INSTRUCTION]
{planner_prompt_text}
[/PLANNER_WORKER_INSTRUCTION]

輸出要求：
- 對話/MD 用繁體中文。
- 指令、路徑、log、代碼用英文原文。
- 每個結論必須附 evidence：file path、line、command 或 log snippet。
- 若證據不足，明確寫「未能驗證」。
- 不要輸出 secrets。
"""


def reporter_prompt(user_text, memory_context, phase_results):
    return f"""你是 Codex reporter。請根據所有 phase 結果整合給 Telegram 的最終回覆。

{MEMORY_RULES}

[ORIGINAL_USER_TASK]
{user_text}
[/ORIGINAL_USER_TASK]

[MEMORY_CONTEXT]
{render_context(memory_context)}
[/MEMORY_CONTEXT]

[PHASE_RESULTS]
{json.dumps(phase_results, ensure_ascii=False, indent=2)[:24000]}
[/PHASE_RESULTS]

請輸出：
- 最終目標
- 結論
- 主要證據
- 風險/未驗證點
- 已保存的 job_id
保持精簡，但不能省略重大失敗或限制。
"""
