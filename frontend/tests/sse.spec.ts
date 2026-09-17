import { expect, test } from "@playwright/test"
import { streamSSE } from "../src/lib/sse"

test("SSE preserves UTF-8 and event boundaries across byte-sized chunks", async () => {
  const oldFetch = globalThis.fetch
  const storage = Object.getOwnPropertyDescriptor(globalThis, "localStorage")
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: { getItem: () => "test-token" },
  })
  const bytes = new TextEncoder().encode(
    ': heartbeat\r\n\r\nevent: model_chunk\r\ndata: {"text":"张三"}\r\n\r\nevent: run_finished\ndata: {"run_id":\ndata: "id"}\n\n',
  )
  let sentHeaders: HeadersInit | undefined
  globalThis.fetch = async (_url, options) => {
    sentHeaders = options?.headers
    return new Response(
      new ReadableStream({
        start(controller) {
          for (const byte of bytes) controller.enqueue(new Uint8Array([byte]))
          controller.close()
        },
      }),
    )
  }
  try {
    const events = []
    for await (const event of streamSSE("http://example.test", {
      body: { message: "hi" },
    }))
      events.push(event)
    expect(events).toEqual([
      { event: "model_chunk", data: { text: "张三" } },
      { event: "run_finished", data: { run_id: "id" } },
    ])
    expect(new Headers(sentHeaders).get("Authorization")).toBe(
      "Bearer test-token",
    )
  } finally {
    globalThis.fetch = oldFetch
    if (storage) Object.defineProperty(globalThis, "localStorage", storage)
    else Reflect.deleteProperty(globalThis, "localStorage")
  }
})

test("SSE surfaces server errors and releases an interrupted reader", async () => {
  const oldFetch = globalThis.fetch
  const storage = Object.getOwnPropertyDescriptor(globalThis, "localStorage")
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: { getItem: () => "test-token" },
  })
  try {
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: "Publish a version" }), {
        status: 400,
      })
    await expect(streamSSE("http://example.test", {}).next()).rejects.toThrow(
      "Publish a version",
    )
    let cancelled = false
    globalThis.fetch = async () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                'event: model_chunk\ndata: {"text":"hi"}\n\n',
              ),
            )
          },
          cancel() {
            cancelled = true
          },
        }),
      )
    for await (const _event of streamSSE("http://example.test", {})) break
    expect(cancelled).toBe(true)
  } finally {
    globalThis.fetch = oldFetch
    if (storage) Object.defineProperty(globalThis, "localStorage", storage)
    else Reflect.deleteProperty(globalThis, "localStorage")
  }
})
