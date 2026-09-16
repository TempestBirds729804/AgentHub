export interface SSEEvent {
  event: string
  data: unknown
}

export async function* streamSSE(
  url: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${localStorage.getItem("access_token") ?? ""}`,
    },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok || !response.body) {
    const error = await response.json().catch(() => null)
    throw new Error(
      typeof error?.detail === "string"
        ? error.detail
        : `Stream failed: ${response.status}`,
    )
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += done
        ? decoder.decode()
        : decoder.decode(value, { stream: true })
      // Normalize only complete CRLF pairs; a CR may end a network chunk.
      buffer = buffer.replace(/\r\n/g, "\n")
      let boundary = buffer.indexOf("\n\n")
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        let event = "message"
        const data: string[] = []
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim()
          if (line.startsWith("data:"))
            data.push(line.slice(5).replace(/^ /, ""))
        }
        if (data.length) yield { event, data: JSON.parse(data.join("\n")) }
        boundary = buffer.indexOf("\n\n")
      }
      if (done) break
    }
    if (buffer.trim()) throw new Error("Stream ended with an incomplete event")
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
