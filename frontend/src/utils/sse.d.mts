export class SSEDecoder {
  constructor(maxBufferedChars?: number)
  push(bytes: Uint8Array): string[]
  finish(): string[]
}
