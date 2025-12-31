from strands import tool
import asyncio
import boto3
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx
from typing import Optional

# EventBridge Scheduler クライアントの初期化
scheduler_client = boto3.client('scheduler')


def _parse_relative_time(relative_str: str) -> timedelta:
    """'+30m', '+2h', '+1d' 形式の相対時間をパース

    Args:
        relative_str: 相対時間文字列 (例: '+30m', '+2h', '+1d')

    Returns:
        timedelta オブジェクト
    """
    match = re.match(r'\+(\d+)([mhd])', relative_str.lower())
    if not match:
        raise ValueError(f"Invalid relative time format: {relative_str}. Use '+30m', '+2h', '+1d' etc.")

    value = int(match.group(1))
    unit = match.group(2)

    if unit == 'm':
        return timedelta(minutes=value)
    elif unit == 'h':
        return timedelta(hours=value)
    elif unit == 'd':
        return timedelta(days=value)
    else:
        raise ValueError(f"Unknown time unit: {unit}")


def _update_schedule_sync(next_datetime: datetime, timezone: str, next_input: str = None) -> dict:
    """get_schedule → update_schedule を同期的に実行

    Args:
        next_datetime: 次回実行日時
        timezone: タイムゾーン
        next_input: 次回実行時のエージェントへの入力（省略時は変更なし）

    Returns:
        更新結果の辞書
    """
    schedule_name = os.environ.get("SCHEDULE_NAME")
    group_name = os.environ.get("SCHEDULE_GROUP_NAME", "default")

    if not schedule_name:
        raise ValueError("SCHEDULE_NAME environment variable is not set")

    # 既存のスケジュール設定を取得
    existing = scheduler_client.get_schedule(
        Name=schedule_name,
        GroupName=group_name
    )

    # at() 形式でスケジュール式を作成
    at_expression = f"at({next_datetime.strftime('%Y-%m-%dT%H:%M:%S')})"

    # Target をコピーして更新
    target = existing['Target'].copy()

    # next_input が指定されている場合、Target.Input 内の Payload.input を更新
    if next_input is not None:
        # Target.Input は二重にJSON化されている構造:
        # { "AgentRuntimeArn": "...", "Payload": "{\"action\":\"start\",\"input\":\"...\"}" }
        original_input = json.loads(target['Input'])
        payload = json.loads(original_input['Payload'])
        payload['input'] = next_input
        original_input['Payload'] = json.dumps(payload, ensure_ascii=False)
        target['Input'] = json.dumps(original_input, ensure_ascii=False)

    # 更新パラメータを構築（既存設定を保持）
    update_params = {
        'Name': schedule_name,
        'GroupName': group_name,
        'ScheduleExpression': at_expression,
        'ScheduleExpressionTimezone': timezone,
        'FlexibleTimeWindow': existing['FlexibleTimeWindow'],
        'Target': target,
    }

    # オプションフィールドを保持
    optional_fields = ['Description', 'EndDate', 'StartDate', 'State', 'KmsKeyArn', 'ActionAfterCompletion']
    for field in optional_fields:
        if field in existing and existing[field] is not None:
            update_params[field] = existing[field]

    # スケジュールを更新
    response = scheduler_client.update_schedule(**update_params)

    return {
        'schedule_arn': response['ScheduleArn'],
        'new_expression': at_expression,
        'timezone': timezone,
        'schedule_name': schedule_name,
        'group_name': group_name,
        'next_input': next_input
    }

@tool
async def http_get(url: str, timeout_sec: int = 10, max_bytes: int = 50_000) -> str:
    """Fetch text from a URL. Use for retrieving web pages or JSON APIs.
    Args:
        url: Target URL (http/https)
        timeout_sec: Request timeout seconds
        max_bytes: Max bytes to read to avoid huge payloads (default 50KB)
    Returns:
        Response text (truncated to max_bytes)
    """
    async with httpx.AsyncClient(timeout=timeout_sec, follow_redirects=True, headers={
        "User-Agent": "AgentCore-Strands-MVP/1.0"
    }) as client:
        r = await client.get(url)
        r.raise_for_status()
        content = r.text
        if len(content) > max_bytes:
            content = content[:max_bytes] + "\n...[truncated]..."
        return content

@tool
async def sleep_seconds(seconds: int = 3) -> str:
    """Sleep for N seconds, then report how long we slept."""
    await asyncio.sleep(max(0, int(seconds)))
    return f"Slept {seconds} seconds"

@tool
def current_time(tz: str = "Asia/Tokyo") -> str:
    """Return the current time in ISO8601 for the given timezone."""
    return datetime.now(ZoneInfo(tz)).isoformat()


@tool
async def update_next_schedule(
    next_execution: str,
    next_input: str = None,
    timezone: str = "Asia/Tokyo"
) -> str:
    """Update the EventBridge Scheduler to set the next execution time and input for this agent.
    Use this to schedule when this agent should run next and what instruction it should receive.

    Args:
        next_execution: Next execution time. Accepts either:
            - ISO8601 datetime string (e.g., "2025-12-27T10:30:00")
            - Relative time like "+30m" (30 minutes), "+2h" (2 hours), "+1d" (1 day)
        next_input: Instruction/input for the next execution (optional).
            Use this to tell your future self what to do next time.
            Example: "This is execution #2. Fetch the latest AWS news."
        timezone: IANA timezone for the schedule expression (default: Asia/Tokyo)

    Returns:
        Confirmation message with the updated schedule details
    """
    try:
        tz = ZoneInfo(timezone)
        now = datetime.now(tz)

        # next_execution をパース（ISO8601形式または相対形式）
        if next_execution.startswith('+'):
            delta = _parse_relative_time(next_execution)
            next_dt = now + delta
        else:
            # ISO8601形式としてパース
            next_dt = datetime.fromisoformat(next_execution)
            if next_dt.tzinfo is None:
                next_dt = next_dt.replace(tzinfo=tz)

        # 検証: 次回実行は未来でなければならない
        if next_dt <= now:
            return f"Error: Next execution time must be in the future. Provided: {next_dt.isoformat()}, Current: {now.isoformat()}"

        # スレッドプールで同期的なboto3呼び出しを実行
        result = await asyncio.to_thread(_update_schedule_sync, next_dt, timezone, next_input)

        response_parts = [
            "Schedule updated successfully!",
            f"- Schedule: {result['schedule_name']} (group: {result['group_name']})",
            f"- Next execution: {result['new_expression']}",
            f"- Timezone: {result['timezone']}",
        ]

        if result.get('next_input'):
            response_parts.append(f"- Next input: {result['next_input'][:100]}{'...' if len(result['next_input']) > 100 else ''}")

        response_parts.append(f"- ARN: {result['schedule_arn']}")

        return "\n".join(response_parts)

    except Exception as e:
        return f"Failed to update schedule: {str(e)}"
