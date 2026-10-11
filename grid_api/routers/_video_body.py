# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bound native video bodies before parsing; never echo private media on errors."""

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

MAX_BODY_BYTES = 64 * 1024 * 1024 + 64 * 1024
PRIVATE = {"Cache-Control": "no-store"}


class BoundedVideoRoute(APIRoute):
    @staticmethod
    def body_limit() -> int:
        return MAX_BODY_BYTES

    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request: Request):
            try:
                if request.method == "POST":
                    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise HTTPException(415, detail="Content-Type must be application/json")
                    limit = self.body_limit()
                    length = request.headers.get("content-length")
                    if length is not None:
                        if not length.isascii() or not length.isdigit() or len(length) > 20:
                            raise HTTPException(400, detail="Invalid Content-Length")
                        if int(length) > limit:
                            raise HTTPException(413, detail="Video request exceeds the body limit")
                    content = bytearray()
                    async for chunk in request.stream():
                        if len(content) + len(chunk) > limit:
                            raise HTTPException(413, detail="Video request exceeds the body limit")
                        content.extend(chunk)
                    request._body = bytes(content)
                response = await original(request)
                response.headers.update(PRIVATE)
                return response
            except RequestValidationError:
                return JSONResponse({"detail": "Invalid video request"}, status_code=422, headers=PRIVATE)
            except RecursionError:
                return JSONResponse({"detail": "Video request exceeds JSON nesting limit"}, status_code=422, headers=PRIVATE)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                    headers={**(exc.headers or {}), **PRIVATE})

        return guarded
