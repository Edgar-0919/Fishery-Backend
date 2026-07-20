"""OneNET 平台回调接口：URL 验证 + 数据接收"""

import hashlib
import base64
import json
from urllib.parse import unquote

from fastapi import APIRouter, Request, HTTPException, Query
from starlette.responses import PlainTextResponse

from app.core.config import settings
from app.api.v1.deps import get_data_processor

router = APIRouter()


@router.get("/callback")
async def verify_url(msg: str = Query(...), nonce: str = Query(...), signature: str = Query(...)):
    decoded_signature = unquote(signature)
    str_a = f"{settings.ONENET_TOKEN}{nonce}{msg}"
    md5_hash = hashlib.md5(str_a.encode('utf-8')).digest()
    calc_sign = base64.b64encode(md5_hash).decode('utf-8')

    if calc_sign != decoded_signature:
        print("签名验证失败")
        raise HTTPException(status_code=401, detail="验证失败")

    return PlainTextResponse(content=msg)


@router.post("/callback")
async def receive_data(request: Request):
    body = await request.json()
    print(f"收到推送: {json.dumps(body, ensure_ascii=False)}")

    try:
        msg_str = body.get('msg')
        if isinstance(msg_str, str):
            msg = json.loads(msg_str)
        else:
            msg = msg_str

        data_processor = get_data_processor()
        await data_processor.process_one_message(msg)

        return {"status": "ok"}

    except Exception as e:
        print(f"处理失败: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "msg": str(e)}