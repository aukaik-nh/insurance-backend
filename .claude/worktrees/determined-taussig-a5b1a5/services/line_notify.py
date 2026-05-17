import os
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"

scheduler = AsyncIOScheduler(timezone="Asia/Bangkok")


async def _push(text: str):
    token   = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.getenv("LINE_NOTIFY_USER_ID", "")
    if not token or not user_id:
        print("[LINE] ยังไม่ได้ตั้งค่า LINE_CHANNEL_ACCESS_TOKEN หรือ LINE_NOTIFY_USER_ID")
        return
    async with httpx.AsyncClient() as client:
        res = await client.post(
            LINE_PUSH_API,
            headers={"Authorization": f"Bearer {token}"},
            json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        )
        print(f"[LINE] push → {res.status_code}")


async def notify_early_morning():
    await _push("🌄 สวัสดีตอนเช้านะครับ ☀️")


async def notify_morning():
    await _push("🌅 เช้าแล้ว! ทำงานได้แล้วนะครับ 💼")


async def notify_noon():
    await _push("🍽️ เที่ยงแล้ว! อย่าลืมพาลูกไปกินข้าวด้วยนะครับ 😊")


def start_scheduler():
    scheduler.add_job(notify_early_morning, CronTrigger(hour=6,  minute=40))
    scheduler.add_job(notify_morning, CronTrigger(hour=9,  minute=0))
    scheduler.add_job(notify_noon,    CronTrigger(hour=12, minute=0))
    scheduler.start()
    print("[scheduler] ✓ ตั้งแจ้งเตือน 09:00 และ 12:00 เรียบร้อย")
