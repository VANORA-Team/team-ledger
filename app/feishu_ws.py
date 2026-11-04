"""Dispatch card frames on SDK releases that otherwise drop them."""
import base64
import http
import time

from lark_oapi.ws.client import Client as SDKClient, _get_by_key
from lark_oapi.ws.const import HEADER_TYPE, HEADER_MESSAGE_ID, HEADER_SUM, HEADER_SEQ, HEADER_BIZ_RT
from lark_oapi.ws.enum import MessageType
from lark_oapi.ws.model import Response
from lark_oapi.core.json import JSON


class Client(SDKClient):
    async def _handle_data_frame(self, frame):
        if _get_by_key(frame.headers, HEADER_TYPE) != MessageType.CARD.value:
            return await super()._handle_data_frame(frame)
        payload = frame.payload
        if int(_get_by_key(frame.headers, HEADER_SUM)) > 1:
            payload = self._combine(_get_by_key(frame.headers, HEADER_MESSAGE_ID),
                                    int(_get_by_key(frame.headers, HEADER_SUM)),
                                    int(_get_by_key(frame.headers, HEADER_SEQ)), payload)
            if payload is None:
                return
        response = Response(code=http.HTTPStatus.OK)
        started = time.monotonic()
        try:
            result = self._event_handler._do_without_validation(payload)
            if result is not None:
                response.data = base64.b64encode(JSON.marshal(result).encode('utf-8'))
        except Exception:
            response = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)
        frame.headers.add(key=HEADER_BIZ_RT, value=str(int((time.monotonic() - started) * 1000)))
        frame.payload = JSON.marshal(response).encode('utf-8')
        await self._write_message(frame.SerializeToString())
