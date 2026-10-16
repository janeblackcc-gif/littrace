/** Stateful UTF-8 + SSE framing. EOF flush is not permission to accept a truncated event. */
export class SSEDecoder {
  constructor(maxBufferedChars = 8 * 1024 * 1024) {
    this.decoder = new TextDecoder('utf-8', { fatal: true })
    this.line = ''
    this.data = []
    this.sawCR = false
    this.closed = false
    this.maxBufferedChars = maxBufferedChars
  }
  push(bytes) {
    if (this.closed) throw new Error('SSE decoder already closed')
    if (!(bytes instanceof Uint8Array)) throw new TypeError('Expected Uint8Array')
    return this.consume(this.decoder.decode(bytes, { stream: true }))
  }
  finish() {
    if (this.closed) throw new Error('SSE decoder already closed')
    this.closed = true
    const events = this.consume(this.decoder.decode()) // flush an incomplete UTF-8 sequence; fatal=true rejects it
    if (this.line.length || this.data.length) throw new Error('Truncated SSE frame at EOF')
    return events
  }
  consume(text) {
    const events = []
    const endLine = () => {
      const line = this.line
      this.line = ''
      if (!line) {
        if (this.data.length) events.push(this.data.join('\n'))
        this.data = []
      } else if (line === 'data' || line.startsWith('data:')) {
        let value = line === 'data' ? '' : line.slice(5)
        if (value.startsWith(' ')) value = value.slice(1)
        this.data.push(value)
      }
      // comments, id, event and retry fields do not alter the app's JSON data payload.
    }
    for (const character of text) {
      if (this.sawCR) {
        this.sawCR = false
        if (character === '\n') continue
      }
      if (character === '\r') { endLine(); this.sawCR = true }
      else if (character === '\n') endLine()
      else this.line += character
      if (this.line.length + this.data.reduce((n, s) => n + s.length, 0) > this.maxBufferedChars)
        throw new Error('SSE frame exceeds configured buffer limit')
    }
    return events
  }
}
