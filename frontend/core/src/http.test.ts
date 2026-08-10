import { describe, expect, it, vi } from 'vitest'
import { HttpTransport } from './http'
import { TransportError, topicMatches } from './transport'

/**
 * No test in this file reaches a network. `fetch` and `EventSource` are both
 * injected, which is also the only reason the transport takes them as options.
 */

function jsonResponse(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'content-type': 'application/json', ...(init.headers ?? {}) },
  })
}

describe('HttpTransport.call', () => {
  it('posts keyword arguments and unwraps the envelope', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true, result: { total: 7 } }))
    const transport = new HttpTransport({ fetch: fetchMock as never })

    await expect(transport.call('appraisal.appraise_bag', { escalate: false })).resolves.toEqual({
      total: 7,
    })

    const [url, init] = fetchMock.mock.calls[0]! as unknown as [string, RequestInit]
    expect(url).toBe('/api/call/appraisal.appraise_bag')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ escalate: false })
  })

  it('turns a backend error into a TransportError with its kind', async () => {
    const transport = new HttpTransport({
      fetch: (async () =>
        jsonResponse(
          { ok: false, error: { kind: 'LeagueUnknownError', message: 'no league' } },
          { status: 400 },
        )) as never,
    })
    await expect(transport.call('x.y')).rejects.toMatchObject({
      kind: 'LeagueUnknownError',
      message: 'no league',
    })
  })

  it('carries Retry-After through so the UI can count down against the real number', async () => {
    const transport = new HttpTransport({
      fetch: (async () =>
        jsonResponse(
          { ok: false, error: { kind: 'RateLimited', message: 'slow down', retry_after: 47 } },
          { status: 429, headers: { 'retry-after': '48' } },
        )) as never,
    })
    const error = await transport.call('x.y').catch((caught: TransportError) => caught)
    expect(error).toBeInstanceOf(TransportError)
    expect((error as TransportError).restricted).toBe(true)
    expect((error as TransportError).retryAfter).toBe(47)
  })

  it('says the backend is unreachable rather than throwing a fetch error at a screen', async () => {
    const transport = new HttpTransport({
      fetch: (async () => {
        throw new TypeError('Failed to fetch')
      }) as never,
    })
    await expect(transport.call('x.y')).rejects.toMatchObject({ kind: 'Unreachable' })
  })

  it('refuses to guess at a response that is not an envelope', async () => {
    const transport = new HttpTransport({ fetch: (async () => jsonResponse({ total: 7 })) as never })
    await expect(transport.call('x.y')).rejects.toMatchObject({ kind: 'MalformedResponse' })
  })
})

class FakeEventSource {
  onerror: ((event: Event) => void) | null = null
  onopen: ((event: Event) => void) | null = null
  closed = false
  private listeners: ((event: MessageEvent) => void)[] = []

  constructor(readonly url: string) {}

  addEventListener(_type: string, listener: (event: MessageEvent) => void) {
    this.listeners.push(listener)
  }

  close() {
    this.closed = true
  }

  deliver(data: string) {
    for (const listener of this.listeners) listener({ data } as MessageEvent)
  }
}

describe('HttpTransport events', () => {
  it('delivers a bus event to a matching subscriber and no other', () => {
    let source: FakeEventSource | undefined
    const transport = new HttpTransport({
      eventSource: (url) => (source = new FakeEventSource(url)),
    })
    const heard: string[] = []
    transport.on('sync_complete', (event) => heard.push(`exact:${event.topic}`))
    transport.on('gamelog.*', (event) => heard.push(`prefix:${event.topic}`))
    transport.on('*', (event) => heard.push(`all:${event.topic}`))
    transport.connect()

    source!.deliver(JSON.stringify({ topic: 'sync_complete', payload: { rows: 3 } }))
    source!.deliver(JSON.stringify({ topic: 'gamelog.zone_entered', payload: {} }))

    expect(heard).toEqual([
      'exact:sync_complete',
      'all:sync_complete',
      'prefix:gamelog.zone_entered',
      'all:gamelog.zone_entered',
    ])
  })

  it('survives a listener that throws, because an event is a notification', () => {
    let source: FakeEventSource | undefined
    const transport = new HttpTransport({
      eventSource: (url) => (source = new FakeEventSource(url)),
    })
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const heard: string[] = []
    transport.on('*', () => {
      throw new Error('broken')
    })
    transport.on('*', (event) => heard.push(event.topic))
    transport.connect()
    source!.deliver(JSON.stringify({ topic: 'sync_complete', payload: {} }))
    expect(heard).toEqual(['sync_complete'])
  })

  it('ignores a malformed frame instead of tearing the stream down', () => {
    let source: FakeEventSource | undefined
    const transport = new HttpTransport({
      eventSource: (url) => (source = new FakeEventSource(url)),
    })
    const heard: string[] = []
    transport.on('*', (event) => heard.push(event.topic))
    transport.connect()
    source!.deliver('not json')
    source!.deliver(JSON.stringify({ nope: true }))
    source!.deliver(JSON.stringify({ topic: 'ok', payload: {} }))
    expect(heard).toEqual(['ok'])
  })

  it('reports its connection state so the shell can say "live" honestly', () => {
    let source: FakeEventSource | undefined
    const transport = new HttpTransport({
      eventSource: (url) => (source = new FakeEventSource(url)),
      setTimeout: (() => 0) as never,
    })
    const states: string[] = []
    transport.onStateChange((state) => states.push(state))
    transport.connect()
    source!.onopen?.(new Event('open'))
    source!.onerror?.(new Event('error'))
    transport.close()
    expect(states).toEqual(['connecting', 'open', 'error', 'closed'])
  })

  it('does not reconnect after the caller closed it', () => {
    const built: FakeEventSource[] = []
    const timers: (() => void)[] = []
    const transport = new HttpTransport({
      eventSource: (url) => {
        const source = new FakeEventSource(url)
        built.push(source)
        return source
      },
      setTimeout: ((fn: () => void) => {
        timers.push(fn)
        return 0
      }) as never,
    })
    transport.connect()
    transport.close()
    built[0]!.onerror?.(new Event('error'))
    for (const fire of timers) fire()
    expect(built).toHaveLength(1)
  })
})

describe('topicMatches', () => {
  it('mirrors the Python bus: exact, prefix.*, or *', () => {
    expect(topicMatches('a.b', 'a.b')).toBe(true)
    expect(topicMatches('a.*', 'a.b')).toBe(true)
    expect(topicMatches('a.*', 'ab')).toBe(false)
    expect(topicMatches('*', 'anything')).toBe(true)
    expect(topicMatches('a.b', 'a.c')).toBe(false)
  })
})
