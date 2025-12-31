import asyncio
import time
import threading

async def demo():
    print(f"[{time.time():.2f}] タスク開始 (スレッド: {threading.current_thread().name})")

    # 同期処理をスレッドで実行
    def heavy_work(n):
        print(f"[{time.time():.2f}]   heavy_work({n}) 開始 (スレッド: {threading.current_thread().name})")
        time.sleep(2)  # 重い処理のシミュレーション
        print(f"[{time.time():.2f}]   heavy_work({n}) 完了")
        return f"結果{n}"

    result = await asyncio.to_thread(heavy_work, 1)
    print(f"[{time.time():.2f}] タスク完了: {result}")

async def main():
    # 3つの並行タスク
    await asyncio.gather(
        demo(),
        demo(),
        demo(),
    )

asyncio.run(main())