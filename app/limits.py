from fastapi import HTTPException
from starlette.responses import JSONResponse


class RequestSizeLimit:
    """Bound multipart spool size and JSON body size, including chunked transfers."""
    def __init__(self, app, max_bytes=13 * 1024 * 1024):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = self.max_bytes + 1
        if length > self.max_bytes:
            return await JSONResponse({'detail': '请求体不能超过 13 MiB'}, status_code=413)(scope, receive, send)
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get('body', b''))
            if total > self.max_bytes:
                raise HTTPException(413, '请求体不能超过 13 MiB')
            return message

        return await self.app(scope, limited_receive, send)
