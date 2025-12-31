# Async Agent with SNS Notification

AWS Bedrock AgentCoreを使用した非同期エージェント実装で、処理完了時にAmazon SNSで通知を送信します。

## 機能

- **非同期処理**: Lambda関数から呼び出し後、即座に応答を返し、バックグラウンドでエージェント処理を実行
- **SNS通知**: エージェント処理完了後（成功・失敗両方）に自動的にSNS通知を送信
- **長時間実行**: HealthyBusyステータスにより、15分のアイドルタイムアウトを回避

## 環境変数

### 必須

- `SNS_TOPIC_ARN`: 通知先のSNS Topic ARN
  - 例: `arn:aws:sns:ap-northeast-1:123456789012:agentcore-notifications`

### オプション

- `BEDROCK_MODEL_ID`: 使用するBedrockモデルID（デフォルト: `jp.anthropic.claude-sonnet-4-5-20250929-v1:0`）

## IAMポリシー

AgentCoreのECSタスクロールに以下のポリシーを追加してください：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sns:Publish"
      ],
      "Resource": "arn:aws:sns:ap-northeast-1:123456789012:agentcore-notifications"
    }
  ]
}
```

## 通知フォーマット

### 成功時

```json
{
  "job_id": "daily-report",
  "status": "success",
  "message": "エージェント処理が正常に完了しました",
  "timestamp": 1234567890.123,
  "result": "エージェントの実行結果..."
}
```

### エラー時

```json
{
  "job_id": "daily-report",
  "status": "error",
  "message": "エージェント処理中にエラーが発生しました: Exception details...",
  "timestamp": 1234567890.123
}
```

## 使用例

### Lambda関数から呼び出し

```python
import boto3

client = boto3.client("bedrock-agentcore")

resp = client.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:...",
    runtimeSessionId="cron-xxx",
    payload=json.dumps({
        "action": "start",
        "job_id": "daily-report",
        "input": "Generate analytics report"
    }).encode()
)
```

### 処理フロー

```
1. Lambda呼び出し（5秒で終了）
   ↓
2. AgentCore起動・タスク登録
   ↓
3. バックグラウンドでエージェント実行（数分〜数時間）
   ↓
4. エージェント処理完了
   ↓
5. SNS通知送信 ← ここ！
   ↓
6. セッション解放
```

## ログ確認

CloudWatch Logsで以下のログを確認できます：

```
[AsyncAgent(SDK)] job=daily-report | start background | input=Generate analytics report
[AsyncAgent] job=daily-report | completed | result=...
[SNS] 通知送信完了: job=daily-report, status=success
[AsyncAgent] job=daily-report | task completed and session released
```

## トラブルシューティング

### SNS通知が送信されない

1. 環境変数`SNS_TOPIC_ARN`が設定されているか確認
2. IAMロールに`sns:Publish`権限があるか確認
3. CloudWatch Logsで`[SNS]`タグのログを確認

### タイムアウトエラー

- HealthyBusyステータス中は時間制限なし
- アイドル状態が15分続くと自動終了
- エージェント処理中は常にHealthyBusyのため問題なし
