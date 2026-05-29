"""
routes/notify.py — ทดสอบส่งข้อความ LINE
"""
from fastapi import APIRouter, HTTPException
from services.line_notify import _broadcast

router = APIRouter()


@router.post("/notify/test")
async def send_test_broadcast(data: dict = None):
    """ยิงข้อความทดสอบให้ทุกคนที่ add bot เป็นเพื่อน

    Body (optional): {"text": "ข้อความที่อยากส่ง"}
    """
    text = (data or {}).get("text") or "🧪 ทดสอบ broadcast — ทุกคนที่เป็นเพื่อนกับ bot จะได้รับข้อความนี้"
    try:
        await _broadcast(text)
        return {"success": True, "text": text}
    except Exception as e:
        raise HTTPException(500, f"ส่งล้มเหลว: {e}")


@router.post("/notify/morning")
async def send_morning():
    """ยิง 'เช้าแล้ว' ทันทีไม่รอ 09:00"""
    await _broadcast("🌅 เช้าแล้ว! ทำงานได้แล้วนะครับ 💼")
    return {"success": True}


@router.post("/notify/noon")
async def send_noon():
    """ยิง 'เที่ยงแล้ว' ทันทีไม่รอ 12:00"""
    await _broadcast("🍽️ เที่ยงแล้ว! อย่าลืมพาลูกไปกินข้าวด้วยนะครับ 😊")
    return {"success": True}
