import os
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

LINE_BROADCAST_API = "https://api.line.me/v2/bot/message/broadcast"
LINE_MULTICAST_API = "https://api.line.me/v2/bot/message/multicast"
LINE_PUSH_API      = "https://api.line.me/v2/bot/message/push"

scheduler = AsyncIOScheduler(timezone="Asia/Bangkok")


async def _broadcast(text: str):
    """ส่งข้อความถึงผู้ใช้ทุกคนที่ add bot เป็นเพื่อน
    ใช้ LINE Broadcast API — ไม่ต้องเก็บ user_id เอง"""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        print("[LINE] ยังไม่ได้ตั้งค่า LINE_CHANNEL_ACCESS_TOKEN")
        return

    # ถ้ามี LINE_NOTIFY_USER_IDS (comma-separated) → ใช้ multicast เฉพาะคนนั้นๆ
    # ถ้ามี LINE_NOTIFY_USER_ID เดียว → ส่งเฉพาะคนนั้น (legacy fallback)
    # ไม่งั้น → broadcast ส่งให้ทุกคน
    multi_ids = os.getenv("LINE_NOTIFY_USER_IDS", "").strip()
    single_id = os.getenv("LINE_NOTIFY_USER_ID", "").strip()

    headers  = {"Authorization": f"Bearer {token}"}
    messages = [{"type": "text", "text": text}]

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            if multi_ids:
                ids = [x.strip() for x in multi_ids.split(",") if x.strip()]
                # LINE multicast = max 500 users per call
                BATCH = 500
                for i in range(0, len(ids), BATCH):
                    res = await client.post(
                        LINE_MULTICAST_API,
                        headers=headers,
                        json={"to": ids[i:i+BATCH], "messages": messages},
                    )
                    print(f"[LINE] multicast ({len(ids[i:i+BATCH])} users) → {res.status_code}")
            elif single_id:
                # legacy: single user push (เพื่อ backward compat)
                res = await client.post(
                    LINE_PUSH_API,
                    headers=headers,
                    json={"to": single_id, "messages": messages},
                )
                print(f"[LINE] push (single user) → {res.status_code}")
            else:
                # ✅ default — broadcast ถึงทุกคนที่ add bot
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
    scheduler.add_job(notify_early_morning, CronTrigger(hour=6,  minute=40))
    scheduler.add_job(notify_morning,       CronTrigger(hour=9,  minute=0))
    scheduler.add_job(notify_noon,          CronTrigger(hour=12, minute=0))
    scheduler.start()
    print("[scheduler] ✓ ตั้งแจ้งเตือน 06:40, 09:00, 12:00 เรียบร้อย")
