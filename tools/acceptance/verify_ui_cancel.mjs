/**
 * D-02 真实前端取消与断连验收脚本。
 *
 * 依据 MIGRATION_RELEASE_PLAN_v1.md §5 (D-02):
 * - 真实 UI 发起取消到真正后台（不是只调用测试函数）；
 * - 确认 Agent 正在运行后再点取消；
 * - 验证 cancel API 响应、SSE 流终止、任务回收、锁释放与数据库终态；
 * - 不得把客户端取消称为云端立即停止计费；
 * - 验证无残留 advisory 锁。
 */
import { pathToFileURL, fileURLToPath } from 'node:url'
import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..', '..')
const FRONTEND = process.env.LITTRACE_FRONTEND || path.join(REPO, 'frontend')
const pwEntry = path.join(FRONTEND, 'node_modules', 'playwright', 'index.mjs')
if (!existsSync(pwEntry)) {
  console.error(`未找到 playwright：${pwEntry}`)
  process.exit(2)
}
const { chromium } = await import(pathToFileURL(pwEntry).href)

const WEB = process.env.LITTRACE_WEB || 'http://localhost:5183'
const API = process.env.LITTRACE_BASE || 'http://127.0.0.1:8000'

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok: !!ok, detail })
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

async function waitForCheckpoint(token, sessionId, predicate, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  let last = null
  while (Date.now() < deadline) {
    const response = await fetch(`${API}/research/checkpoint/${sessionId}/full`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (response.ok) {
      const body = await response.json()
      last = body.checkpoint
      if (predicate(last)) return last
    }
    await sleep(250)
  }
  throw new Error(`等待取消终态超时，最后 checkpoint=${JSON.stringify(last)}`)
}

function probeAdvisoryLock(sessionId) {
  const source = String.raw`
import hashlib, json, os, sys
from pathlib import Path
base = Path.cwd()
sys.path.insert(0, str(base / "backend" / "app"))
from dotenv import load_dotenv
load_dotenv(base / ".env")
from sqlalchemy import text
from core.database import engine
key = int.from_bytes(hashlib.sha256(("research:" + sys.argv[1]).encode()).digest()[:8], "big", signed=True)
unsigned = key & ((1 << 64) - 1)
with engine.connect() as conn:
    count = conn.execute(text("""
        SELECT count(*) FROM pg_locks
        WHERE locktype='advisory' AND classid=:hi AND objid=:lo
          AND objsubid=1 AND granted
    """), {"hi": unsigned >> 32, "lo": unsigned & 0xffffffff}).scalar_one()
print(json.dumps({"session_id": sys.argv[1], "granted_advisory_locks": count}))
`
  const probe = spawnSync(process.env.PYTHON || 'python', ['-c', source, sessionId], {
    cwd: REPO, encoding: 'utf8', env: process.env,
  })
  if (probe.status !== 0) {
    throw new Error(`PG advisory lock 探针失败(${probe.status}): ${probe.stderr || probe.stdout}`)
  }
  return JSON.parse(probe.stdout.trim())
}

async function seedUser() {
  const tag = Math.random().toString(36).slice(2, 10)
  const body = { username: `uic_${tag}`, email: `uic_${tag}@example.com`, password: 'UiC-Cancel-Passw0rd!' }
  const r = await fetch(`${API}/auth/register`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`seed 注册失败 ${r.status}: ${await r.text()}`)
  return body
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })

await page.addInitScript(() => {
  window.__sseEvents = []
  const original = window.fetch.bind(window)
  window.fetch = async (...args) => {
    const res = await original(...args)
    try {
      const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || ''
      if (url.includes('/research/stream') && res.body) {
        const clone = res.clone()
        ;(async () => {
          const reader = clone.body.getReader()
          const decoder = new TextDecoder('utf-8')
          let buffer = ''
          for (;;) {
            const { value, done } = await reader.read()
            if (done) break
            buffer += decoder.decode(value, { stream: true })
            const lines = buffer.split('\n')
            buffer = lines.pop()
            for (const line of lines) {
              if (line.startsWith('data: ')) {
                try {
                  const ev = JSON.parse(line.slice(6))
                  window.__sseEvents.push(ev)
                } catch (e) {}
              }
            }
          }
        })().catch(() => {})
      }
    } catch (e) {}
    return res
  }
})

page.on('console', m => console.log('  [BROWSER]', m.type(), m.text()))
page.on('request', r => { if (r.url().includes('cancel')) console.log('  [CANCEL REQ]', r.method(), r.url()) })
page.on('response', (res) => {
  if (res.url().includes('cancel')) {
    console.log('  [CANCEL RES]', res.status(), res.url())
  }
})

try {
  const user = await seedUser()
  console.log(`\n=== [D-02] 真实前端取消验收 ===\n前端: ${WEB}\n后端: ${API}\n用户: ${user.username}`)

  // 1. 登录
  await page.goto(`${WEB}/login`, { waitUntil: 'networkidle', timeout: 60000 })
  await page.locator('input#login_username, input[placeholder="用户名或邮箱"]').first().fill(user.username)
  await page.locator('input[type="password"]').first().fill(user.password)
  await page.locator('button[type="submit"]').first().click()
  await page.waitForURL(/\/chat/, { timeout: 30000 })
  check('真实登录成功进入 /chat', /\/chat/.test(page.url()))

  // 2. 建立会话
  await page.waitForLoadState('networkidle')
  const recommended = page.getByText(/Agent 记忆的检索粒度|RAG 长上下文边界|Judge 可靠性/).first()
  if (await recommended.count()) { await recommended.click().catch(() => {}) }
  await page.waitForURL(/\/chat\/[0-9a-f-]{8,}/, { timeout: 30000 }).catch(() => {})
  const sessionUrl = page.url()
  const sessionId = (sessionUrl.match(/\/chat\/([0-9a-f-]{8,})/) || [])[1] || null
  check('会话创建成功', !!sessionId, `sessionId=${sessionId}`)

  // 搜索模式：确保含 web
  const modeButton = page.getByRole('button', { name: /搜索模式|深度搜索|知识库|全部搜索/ }).first()
  const modeText = (await modeButton.textContent().catch(() => '')) || ''
  if (!/深度搜索|全部搜索/.test(modeText)) {
    await modeButton.click()
    await page.waitForTimeout(500)
    const webBox = page.getByRole('checkbox', { name: /深度搜索（网络）|网络/ }).first()
    if (await webBox.count()) { await webBox.check().catch(() => {}) }
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
  }

  // 方向与年份
  const directionSelect = page.locator('select[aria-label="研究方向"]')
  await directionSelect.waitFor({ state: 'attached', timeout: 10000 })
  await directionSelect.selectOption('agent_memory')
  const yearStart = page.locator('input[aria-label="起始年"]')
  const yearEnd = page.locator('input[aria-label="截止年"]')
  if (await yearStart.count()) { await yearStart.fill('2022') }
  if (await yearEnd.count()) { await yearEnd.fill('2025') }
  await page.waitForTimeout(500)

  const input = page.locator('textarea, input[type="text"]').first()
  await input.fill('长程对话问答中，记忆的会话/轮次/事实粒度以及记忆压缩，效果依赖哪些条件？')
  const sendButton = page.getByRole('button', { name: /发送|开始|研究/ }).first()
  if (await sendButton.count()) { await sendButton.click() } else { await input.press('Enter') }

  // 4. 等待 Agent 运行屏障：phase 是调度事件，不足以证明 Agent 已进入。
  // preset 方向的 direction_resolved 由 Architect.process 内部发出，且带本次 run_id。
  console.log('等待 Architect 内部 direction_resolved 运行屏障...')
  await page.waitForFunction(() => (window.__sseEvents || []).some(event =>
    event.type === 'direction_resolved' && !!event.run_id && Number.isInteger(event.event_seq)
  ), undefined, { timeout: 60000 })
  const barrierEvent = await page.evaluate(() =>
    (window.__sseEvents || []).find(event => event.type === 'direction_resolved'))
  const runId = barrierEvent?.run_id
  check('运行屏障确认：Architect.process 已产生 direction_resolved',
    !!runId && barrierEvent.terminal === false,
    `run_id=${runId}, event_seq=${barrierEvent?.event_seq}`)

  // 5. 点击真实前端“停止生成”按钮
  const stopButton = page.locator('button.com-sender__stop-btn, button[title="停止生成"]').first()
  await stopButton.waitFor({ state: 'visible', timeout: 10000 })
  const cancelResponsePromise = page.waitForResponse(response =>
    response.url().includes(`/research/cancel/${sessionId}`)
      && response.request().method() === 'POST', { timeout: 30000 })
  await stopButton.click()
  const cancelResponse = await cancelResponsePromise
  const cancelBody = await cancelResponse.json().catch(() => null)
  console.log('已点击 UI 停止按钮，等待取消链路完成...')
  console.log('  [CANCEL BODY]', JSON.stringify(cancelBody))

  // 6. 验证后端明确接受取消请求，而不是任意 cancel URL / 任意响应。
  check('UI 触发后端取消 API 且返回 200', cancelResponse.status() === 200,
    `status=${cancelResponse.status()}`)
  check('取消 API 响应明确为 success=true', cancelBody?.success === true,
    JSON.stringify(cancelBody))

  // 7. 同一 run 必须收到 research_cancelled 终态，随后 PG 终态也必须匹配。
  await page.waitForFunction(({ expectedRunId }) => (window.__sseEvents || []).some(event =>
    event.run_id === expectedRunId && event.type === 'research_cancelled' && event.terminal === true
  ), { expectedRunId: runId }, { timeout: 30000 })
  const terminalEvents = await page.evaluate(expectedRunId =>
    (window.__sseEvents || []).filter(event => event.run_id === expectedRunId && event.terminal === true), runId)
  check('SSE 对应 run 仅产生 research_cancelled 终态',
    terminalEvents.length === 1 && terminalEvents[0].type === 'research_cancelled',
    JSON.stringify(terminalEvents))

  const token = await page.evaluate(() => {
    try { return JSON.parse(localStorage.getItem('auth') || '{}').token || null } catch { return null }
  })
  if (!token) throw new Error('登录后未取得 auth token，无法核对 PG checkpoint')
  const checkpoint = await waitForCheckpoint(token, sessionId, cp =>
    cp?.status === 'paused'
      && cp?.state_json?.last_terminal_event?.type === 'research_cancelled'
      && cp?.state_json?.last_terminal_event?.run_id === runId)
  console.log('  [CHECKPOINT]', JSON.stringify({
    session_id: checkpoint.session_id,
    status: checkpoint.status,
    phase: checkpoint.phase,
    last_terminal_event: checkpoint.state_json.last_terminal_event,
  }))
  check('PG checkpoint 持久化为 paused/research_cancelled 且 run_id 一致', true)

  // CheckpointGuard 退出后，本 session 的 advisory lock 必须已释放。
  const lockProbe = probeAdvisoryLock(sessionId)
  console.log('  [LOCK PROBE]', JSON.stringify(lockProbe))
  check('取消终态后对应 PG advisory lock 已释放',
    lockProbe.granted_advisory_locks === 0,
    `granted=${lockProbe.granted_advisory_locks}`)

  // 8. 验证 UI 反馈与执行任务终止
  await stopButton.waitFor({ state: 'hidden', timeout: 10000 })
  const bodyText = (await page.locator('body').innerText().catch(() => '')) || ''
  check('前端呈现明确停止状态', bodyText.includes('已停止生成') || bodyText.includes('⏹️'))

  // 9. 验证同一 run 没有虚假成功完成。
  const eventsAfter = await page.evaluate(() => window.__sseEvents || [])
  const completeEvent = eventsAfter.find(e => e.run_id === runId && e.type === 'research_complete')
  check('取消操作后同一 run 未产生虚假的 research_complete 事件', !completeEvent)

  console.log(`\n[D-02 验收汇总]: ${results.filter(r => r.ok).length}/${results.length} PASS`)
  await browser.close()
  process.exit(results.every(r => r.ok) ? 0 : 1)
} catch (err) {
  console.error('D-02 验收异常:', err)
  try { await browser.close() } catch (e) {}
  process.exit(3)
}
