import os
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

LINE_BROADCAST_API = "https://api.line.me/v2/bot/message/broadcast"
LINE_MULTICAST_API = "https://api.line.me/v2/bot/message/multicast"
LINE_PUSH_API      = "https://api.line.me/v2/bot/message/push"

scheduler = AsyncIOScheduler(timezone="Asia/Bangkok")


async def _broadcast(text: str):
    """ส่งข้อความถึงผู้ใช้ทุกคนที่ add bot เป็นเพื่อน (default = broadcast)

    Override ได้ผ่าน env:
      LINE_NOTIFY_USER_IDS=U123,U456  → multicast เฉพาะ list นี้ (override broadcast)
    """
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        print("[LINE] ยังไม่ได้ตั้งค่า LINE_CHANNEL_ACCESS_TOKEN")
        return

    multi_ids = os.getenv("LINE_NOTIFY_USER_IDS", "").strip()
    headers   = {"Authorization": f"Bearer {token}"}
    messages  = [{"type": "text", "text": text}]

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            if multi_ids:
                # explicit override → multicast เฉพาะ list ที่ระบุ
                ids = [x.strip() for x in multi_ids.split(",") if x.strip()]
                BATCH = 500
                for i in range(0, len(ids), BATCH):
                    res = await client.post(
                        LINE_MULTICAST_API,
                        headers=headers,
                        json={"to": ids[i:i+BATCH], "messages": messages},
                    )
                    print(f"[LINE] multicast ({len(ids[i:i+BATCH])} users) → {res.status_code}")
                    if res.status_code >= 400:
                        print(f"[LINE] error body: {res.text[:200]}")
            else:
                # ✅ default — broadcast ส่งให้ทุกคนที่ add bot เป็นเพื่อน
                # (LINE_NOTIFY_USER_ID single ตัวเก่าถูก ignore — ไม่ใช้แล้ว)
                res = await client.post(
                    LINE_BROADCAST_API,
                    headers=headers,
                    json={"messages": messages},
                )
                print(f"[LINE] broadcast (all friends) → {res.status_code}")
                if res.status_code >= 400:
                    print(f"[LINE] error body: {res.text[:200]}")
        except Exception as e:
            print(f"[LINE] error: {e}")


# alias เก่า — keep for backward compat
_push = _broadcast


async def notify_early_morning():
    await _broadcast("🌄 สวัสดีตอนเช้านะครับ ☀️")


async def notify_morning():
    await _broadcast("🌅 เช้าแล้ว! ทำงานได้แล้วนะครับ 💼")


async def notify_noon():
    await _broadcast("🍽️ เที่ยงแล้ว! อย่าลืมพาลูกไปกินข้าวด้วยนะครับ 😊")


def start_scheduler():
    # day_of_week="mon-fri" → เฉพาะวันธรรมดา (ข้ามเสาร์/อาทิตย์)
    weekday = {"day_of_week": "mon-fri"}
    scheduler.add_job(notify_early_morning, CronTrigger(hour=6,  minute=40, **weekday))
    scheduler.add_job(notify_morning,       CronTrigger(hour=9,  minute=0,  **weekday))
    scheduler.add_job(notify_noon,          CronTrigger(hour=12, minute=0,  **weekday))
    scheduler.start()
    print("[scheduler] ✓ ตั้งแจ้งเตือน 06:40, 09:00, 12:00 (จันทร์-ศุกร์เท่านั้น)")
