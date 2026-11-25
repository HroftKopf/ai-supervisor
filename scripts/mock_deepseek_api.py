"""Local OpenAI-compatible mock used only for offline smoke tests.

It never calls a model. Start with:
    python scripts/mock_deepseek_api.py --port 8765
and point DEEPSEEK_BASE_URL=http://127.0.0.1:8765/v1 at it.
"""

from __future__ import annotations

import argparse
import json

from aiohttp import web


async def chat(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    model = str(body.get("model") or "deepseek-v4-flash")
    if body.get("stream"):
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        chunks = [
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "Готово. Изменений не требуется.",
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 10},
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            },
        ]
        for chunk in chunks:
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response
    return web.json_response(
        {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


async def messages(request: web.Request) -> web.StreamResponse:
    """Minimal DeepSeek Messages (Anthropic-compatible) SSE response."""
    body = await request.json()
    model = str(body.get("model") or "deepseek-v4-flash")
    response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Готово. Изменений не требуется."},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 10},
        },
        {"type": "message_stop"},
    ]
    for event in events:
        await response.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
    return response


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/chat/completions", chat)
    app.router.add_post("/v1/messages", messages)
    app.router.add_post("/messages", messages)
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
