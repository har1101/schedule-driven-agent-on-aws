import os, asyncio, logging, json
from typing import Dict, Any
from strands import Agent
from strands.models.bedrock import BedrockModel
from bedrock_agentcore import BedrockAgentCoreApp
import boto3

from tools import http_get, sleep_seconds, current_time, update_next_schedule

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager


log = logging.getLogger("AsyncAgent")
logging.basicConfig(level=logging.INFO)

# SNSクライアントの初期化
sns_client = boto3.client('sns')

app = BedrockAgentCoreApp()

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0")
model = BedrockModel(model_id=MODEL_ID, streaming=False)

# AgentCore Memory 設定（環境変数から取得、デフォルト値あり）
MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")
SESSION_ID = os.environ.get("AGENTCORE_SESSION_ID", "scheduled_agent_session")
ACTOR_ID = os.environ.get("AGENTCORE_ACTOR_ID", "async_agent")
MEMORY_STRATEGY_ID = os.environ.get("AGENTCORE_MEMORY_STRATEGY_ID")

SYSTEM_PROMPT = (
    "You are a pragmatic research agent that runs on a schedule.\n\n"
    "## Memory Context\n"
    "You have access to memory of previous executions. Use this context to:\n"
    "- Understand what was done in previous runs\n"
    "- Avoid repeating the same information\n"
    "- Build upon previous findings\n\n"
    "## Core Behaviors\n"
    "- Think step by step.\n"
    "- Use tools when helpful (http_get, current_time, sleep_seconds, update_next_schedule).\n"
    "- Keep outputs concise unless asked.\n\n"
    "## Execution Rules (Based on Execution Number)\n"
    "Check the input message to identify the execution number (e.g., '1回目', '2回目', '3回目').\n\n"
    "### ODD-numbered executions (1回目, 3回目, 5回目...):\n"
    "1. Return the current time using current_time tool.\n"
    "2. Schedule next execution for 5 minutes later (+5m).\n"
    "3. Set next_input to indicate the next execution number (e.g., 'これは2回目の実行です。').\n\n"
    "### EVEN-numbered executions (2回目, 4回目, 6回目...):\n"
    "1. Search for the latest AWS news/updates using http_get.\n"
    "2. Schedule next execution for 10 minutes later (+10m).\n"
    "3. Set next_input to indicate the next execution number (e.g., 'これは3回目の実行です。').\n\n"
    "## Autonomous Scheduling\n"
    "Before completing your task, you MUST call update_next_schedule with:\n"
    "- next_execution: '+5m' for odd executions, '+10m' for even executions\n"
    "- next_input: 'これはN回目の実行です。' (where N is the next execution number)\n"
)

# メモリー設定を作成（環境変数から取得した値を使用）
memory_config = AgentCoreMemoryConfig(
    memory_id=MEMORY_ID,
    session_id=SESSION_ID,  # 固定値: すべての実行を同じ会話として扱う
    actor_id=ACTOR_ID,      # 固定値: エージェント自体が唯一のアクター
    retrieval_config={
        # summary strategyから前回の実行要約を取得
        f"/strategies/{MEMORY_STRATEGY_ID}/actors/{ACTOR_ID}/sessions/{SESSION_ID}":
            RetrievalConfig(top_k=5, relevance_score=0.3)
    }
)

# セッションマネージャーを作成
session_manager = AgentCoreMemorySessionManager(
    agentcore_memory_config=memory_config
)

agent = Agent(
    model=model,
    tools=[http_get, sleep_seconds, current_time, update_next_schedule],
    system_prompt=SYSTEM_PROMPT,
    session_manager=session_manager
)

# --- SNS通知送信関数 ---
async def send_sns_notification(job_id: str, status: str, message: str, result: Any = None):
    """
    エージェント処理完了後にSNS通知を送信する

    Args:
        job_id: ジョブID
        status: 処理ステータス（"success" or "error"）
        message: 通知メッセージ
        result: エージェントの実行結果（オプション）
    """
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        log.warning("[SNS] SNS_TOPIC_ARN環境変数が設定されていないため、通知をスキップします")
        return

    try:
        # 通知メッセージの作成
        notification_data = {
            "job_id": job_id,
            "status": status,
            "message": message,
            "timestamp": asyncio.get_event_loop().time()
        }

        if result:
            # 結果が長すぎる場合は切り詰める
            result_str = str(result)
            if len(result_str) > 1000:
                result_str = result_str[:1000] + "...(truncated)"
            notification_data["result"] = result_str

        # SNS通知を非同期で送信（スレッドプールで実行）
        await asyncio.to_thread(
            sns_client.publish,
            TopicArn=topic_arn,
            Subject=f"AgentCore Job {status.upper()}: {job_id}",
            Message=json.dumps(notification_data, indent=2, ensure_ascii=False)
        )

        log.info("[SNS] 通知送信完了: job=%s, status=%s", job_id, status)

    except Exception as e:
        log.error("[SNS] 通知送信失敗: %s", e)
        # 通知失敗はエージェント処理の成功/失敗には影響させない

# --- 裏で回る本処理（invoke_async でネイティブに実行）---
async def _background_run(task_id: int, payload: Dict[str, Any], context):
    job_id = payload.get("job_id", "mvp")
    result = None

    try:
        seconds = int(payload.get("seconds", 0) or 0)
        if seconds > 0:
            # ※ "待ち"はイベントループにやらせる（ツール直呼びではなく）
            await asyncio.sleep(seconds)

        user_input = payload.get("input") or "Say hello and show current_time."
        log.info("[AsyncAgent(SDK)] job=%s | start background | input=%s", job_id, user_input)

        # --- ここが変更点：to_thread → invoke_async（非同期ネイティブ） ---
        # タイムアウトを付けたい場合は wait_for(...) でラップ
        result = await agent.invoke_async(user_input)  # ← await で完了まで非ブロッキングに待つ
        log.info("[AsyncAgent] job=%s | completed | result=%s", job_id, str(result)[:1000])

        # エージェント処理成功後にSNS通知を送信
        await send_sns_notification(
            job_id=job_id,
            status="success",
            message="エージェント処理が正常に完了しました",
            result=result
        )

    except Exception as e:
        log.exception("[AsyncAgent(SDK)] job failed: %s", e)

        # エージェント処理失敗時にもSNS通知を送信
        await send_sns_notification(
            job_id=job_id,
            status="error",
            message=f"エージェント処理中にエラーが発生しました: {str(e)}"
        )

    finally:
        app.complete_async_task(task_id)  # セッション解放（必須）
        log.info("[AsyncAgent] job=%s | task completed and session released", job_id)

# --- 即レスするエントリポイント ---
@app.entrypoint
async def main(payload: Dict[str, Any], context=None):
    if payload.get("action") == "start":
        task_id = app.add_async_task("agent_job", {"job_id": payload.get("job_id")})
        asyncio.create_task(_background_run(task_id, payload, context))
        return {"status": "started", "task_id": task_id}
    return {"status": "noop"}

if __name__ == "__main__":
    app.run()
