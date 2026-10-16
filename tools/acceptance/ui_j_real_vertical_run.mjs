/**
 * B. 真实纵向联调：React 表单 → 方向请求 → 检索与正文 → 证据 → 报告 → 审核 → 最终快照。
 *
 * 为什么另写一个脚本：
 *   `ui_i_browser.mjs` 里的探针是**页面内手写 fetch**，那只能证明网络与编码行为，
 *   不是「从实际表单到最终报告」的端到端。本脚本驱动**真实控件**：
 *     1) 真实 `select[aria-label="研究方向"]` 选方向（不是手拼请求体）；
 *     2) 真实 sender 输入问题并发送；
 *     3) 真实 SSE 流由**应用自己**消费；脚本只用 `fetch` 观察钩子 tee 一份事件流
 *        （`res.clone()`，不改变应用读到的数据、不改变流式时序）；
 *     4) 断言报告真的渲染进 DOM，而不是只看网络返回。
 *
 * 需要：后端 8000（当前提交）与前端 5183 都已启动。
 * 用法：node tools/acceptance/ui_j_real_vertical_run.mjs [--direction rag_context] [--query "..."]
 */
import { pathToFileURL, fileURLToPath } from 'node:url'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
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
const argv = process.argv.slice(2)
const arg = (name, dflt) => {
  const i = argv.indexOf(`--${name}`)
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt
}
const DIRECTION = arg('direction', 'rag_context')
const QUERY = arg('query', 'RAG 与长上下文在长文档问答上的边界：检索与长上下文方法的实证对比')
// 时间窗口在真实表单里是**必填整数**（`start_year`/`end_year: number`），没有「不限年份」的
// 真实控件状态，所以不设 `--years none`：那只能靠 API 越过 UI 做到，用它冒充 UI 证据是假的。
const YEARS = arg('years', '2022-2025')
const OUT = arg('out', path.join(REPO, 'docs', 'littrace', 'eval', `real_vertical_${DIRECTION}.json`))
const RUN_TIMEOUT_MS = Number(arg('timeout_ms', String(35 * 60 * 1000)))

const results = []
const infos = []
function check(name, ok, detail = '') {
  results.push({ name, ok: !!ok, detail })
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}
/** 观察项：留证但不计入通过分母（已知失败不能算 PASS）。 */
function info(name, detail = '') {
  infos.push({ name, detail })
  console.log(`  [INFO] ${name}${detail ? ` — ${detail}` : ''}`)
}

async function seedUser() {
  const tag = Math.random().toString(36).slice(2, 10)
  const body = { username: `uib_${tag}`, email: `uib_${tag}@example.com`, password: 'UiB-Browser-Passw0rd!' }
  const r = await fetch(`${API}/auth/register`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`seed 注册失败 ${r.status}: ${await r.text()}`)
  return body
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
const consoleErrors = []
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })
page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`))
// 真实失败请求也留存（代码评审要求失败样例）
const failedRequests = []
page.on('response', (r) => { if (r.status() >= 400) failedRequests.push(`${r.status()} ${r.request().method()} ${r.url()}`) })
page.on('requestfailed', (r) => failedRequests.push(`FAILED ${r.method()} ${r.url()} ${r.failure() && r.failure().errorText}`))

// 观察钩子：只 tee 一份 /research/stream 的事件流，应用仍然自己消费原始响应
await page.addInitScript(() => {
  window.__sseRaw = ''
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
          for (;;) {
            const { value, done } = await reader.read()
            if (done) break
            window.__sseRaw += decoder.decode(value, { stream: true })
          }
        })().catch(() => { /* 观察分支失败不影响应用 */ })
      }
    } catch (e) { /* 钩子自身异常不得影响应用请求 */ }
    return res
  }
})

const started = Date.now()
let requestPayload = null
const eventTypes = []
const streamRequests = []
page.on('request', (req) => {
  if (req.method() === 'POST' && req.url().includes('/research/stream')) {
    streamRequests.push(req.url())
    try { requestPayload = req.postDataJSON() } catch (e) { requestPayload = { parse_error: String(e) } }
  }
})

try {
  const user = await seedUser()
  console.log(`前端: ${WEB}\n后端: ${API}\n用户: ${user.username}\n方向: ${DIRECTION}\n问题: ${QUERY}\n`)

  // ---------- 1. 真实登录 ----------
  console.log('[1] 真实登录表单')
  await page.goto(`${WEB}/login`, { waitUntil: 'networkidle', timeout: 60000 })
  await page.locator('input#login_username, input[placeholder="用户名或邮箱"]').first().fill(user.username)
  await page.locator('input[type="password"]').first().fill(user.password)
  await page.locator('button[type="submit"]').first().click()
  await page.waitForURL(/\/chat/, { timeout: 30000 })
  check('登录后进入 /chat', /\/chat/.test(page.url()), page.url())

  // ---------- 2. 建立会话（方向表单只在会话内渲染）----------
  // 真实 UI 事实：/chat 空态没有研究方向控件；先点一个推荐问题建会话。
  // 修后语义推荐问题**只建会话 + 预填问题**，
  // 不自动发送、不打已下线的 `/chat/completion`，统一由用户确认后走 `/research/stream`。
  console.log('[2] 建立会话并确认方向表单出现')
  await page.waitForLoadState('networkidle')
  const streamBeforeRecommend = streamRequests.length
  const recommended = page.getByText(/Agent 记忆的检索粒度|RAG 长上下文边界|Judge 可靠性/).first()
  if (await recommended.count()) { await recommended.click().catch(() => {}) }
  await page.waitForURL(/\/chat\/[0-9a-f-]{8,}/, { timeout: 30000 }).catch(() => {})
  const directionSelect = page.locator('select[aria-label="研究方向"]')
  await directionSelect.waitFor({ state: 'attached', timeout: 30000 }).catch(() => {})
  await page.waitForTimeout(1500)
  const sessionUrl = page.url()
  const sessionId = (sessionUrl.match(/\/chat\/([0-9a-f-]{8,})/) || [])[1] || null
  check('会话已创建且方向表单出现在真实 DOM', !!sessionId && (await directionSelect.count()) > 0,
        `session=${sessionId}`)
  const legacyChatCalls = failedRequests.filter((f) => f.includes('/chat/'))
  check('推荐入口不再调用已下线的 /chat/completion（修后验收项）',
        legacyChatCalls.length === 0, legacyChatCalls.join(' | ') || '(无 404)')
  check('推荐入口只预填问题、不自动发起研究请求',
        streamRequests.length === streamBeforeRecommend,
        `stream 请求数 ${streamBeforeRecommend} → ${streamRequests.length}`)
  const prefillBox = page.locator('textarea, input[type="text"]').first()
  const prefillValue = (await prefillBox.inputValue().catch(() => '')) || ''
  check('推荐问题已预填进输入框（等待用户确认）', prefillValue.trim().length > 4, `prefill=${prefillValue.slice(0, 40)}`)
  info('推荐问题文本', prefillValue.trim().slice(0, 60))
  if (legacyChatCalls.length) info('失败样例（/chat/* 404）', legacyChatCalls.join(' | '))

  // ---------- 3. 搜索模式：确保含 web（deepsearch 分支）----------
  console.log('[3] 搜索模式：确保含 web（deepsearch 分支）')
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
  const modeAfter = (await modeButton.textContent().catch(() => '')) || ''
  check('搜索模式包含网络检索（否则不会走 deepsearch）', /深度搜索|全部搜索/.test(modeAfter), modeAfter.trim())

  // ---------- 4. 真实表单：选方向 + 年限 ----------
  console.log('[4] 真实表单控件：研究方向 / 时间窗口')
  await directionSelect.selectOption(DIRECTION)
  const yearStart = page.locator('input[aria-label="起始年"]')
  const yearEnd = page.locator('input[aria-label="截止年"]')
  const [yFrom, yTo] = YEARS.split('-')
  if (await yearStart.count()) { await yearStart.fill(yFrom) }
  if (await yearEnd.count()) { await yearEnd.fill(yTo) }
  const startVal = (await yearStart.count()) ? await yearStart.inputValue() : ''
  const endVal = (await yearEnd.count()) ? await yearEnd.inputValue() : ''
  check('方向与时间窗口已通过真实控件设置',
        (await directionSelect.inputValue()) === DIRECTION && startVal === yFrom && endVal === yTo,
        `direction=${await directionSelect.inputValue()} years=${startVal || '(空)'}~${endVal || '(空)'}`)

  // ---------- 5. 发送并等待完成事件 ----------
  console.log('[5] 发送并等待真实流水线完成（含检索/抽取/写作/审核）')
  const input = page.locator('textarea, input[type="text"]').first()
  await input.fill(QUERY)
  const sendButton = page.getByRole('button', { name: /发送|开始|研究/ }).first()
  const send = async () => {
    if (await sendButton.count()) { await sendButton.click() } else { await input.press('Enter') }
  }

  // ---------- 5a. 可失败验收：非法时间窗口必须被表单挡住 ----------
  // 真实缺陷（round2-B1 联调暴露）：清空 number 输入框后 `Number('') === 0`，
  // 旧校验只查「整数且 start<=end」，于是 **0~0** 被当成合法窗口发出去，
  // 后端据此把所有来源判为越界，本轮材料为 0——错误发生在表单，症状出现在证据。
  // 代码评审补充：负例必须真的**清空**（而不是手填 '0'），
  // 并验证「恢复合法值后能正常发送」，否则断言名会超过实际动作。
  console.log('[5a] 负例：非法时间窗口（清空输入框）必须被表单拒绝')
  await yearStart.fill('')
  await yearEnd.fill('')
  const clearedVals = `${await yearStart.inputValue().catch(() => '')}~${await yearEnd.inputValue().catch(() => '')}`
  const streamBefore = streamRequests.length
  await send()
  await page.waitForTimeout(1500)
  const bodyAfterInvalid = (await page.locator('body').innerText().catch(() => '')) || ''
  check('非法时间窗口被表单拒绝且未发出研究请求',
        streamRequests.length === streamBefore && /时间窗口无效|超出支持范围/.test(bodyAfterInvalid),
        `清空后控件值=${clearedVals}；stream 请求数 ${streamBefore} → ${streamRequests.length}；提示=${(
          bodyAfterInvalid.match(/(时间窗口无效[^\n]*|时间窗口超出支持范围[^\n]*)/) || ['(未出现提示)'])[0]}`)
  await yearStart.fill(yFrom)
  await yearEnd.fill(yTo)
  const restoredVals = `${await yearStart.inputValue()}~${await yearEnd.inputValue()}`
  check('恢复合法时间窗口后控件值正确（下一句会真的发出请求）',
        restoredVals === `${yFrom}~${yTo}`, restoredVals)
  await input.fill(QUERY)

  await send()

  // 等待**终态**事件进入观察流（不能见到第一条泛 error 就停止取证）：
  // 只有 research_complete / research_cancelled / error(terminal) 才算这次执行的终态。
  const deadline = Date.now() + RUN_TIMEOUT_MS
  let terminalSeen = false
  while (Date.now() < deadline) {
    const raw = await page.evaluate(() => window.__sseRaw || '').catch(() => '')
    const lines = raw.split('\n').filter((l) => l.startsWith('data: '))
    for (const line of lines) {
      let ev = null
      try { ev = JSON.parse(line.slice(6)) } catch (e) { continue }
      if (ev && ev.terminal === true) { terminalSeen = true }
    }
    if (terminalSeen) break
    await page.waitForTimeout(5000)
    const secs = Math.round((Date.now() - started) / 1000)
    if (secs % 30 < 6) {
      const types = [...raw.matchAll(/"type": ?"([a-z_]+)"/g)].map((m) => m[1])
      console.log(`      … ${secs}s，已见事件 ${types.length} 个，最后=${types.slice(-3).join(',')}`)
    }
  }
  const raw = await page.evaluate(() => window.__sseRaw || '').catch(() => '')
  const dataLines = raw.split('\n').filter((l) => l.startsWith('data: '))
  const parsedLines = dataLines.map((l) => {
    try { return { ok: true, event: JSON.parse(l.slice(6)) } } catch (e) { return { ok: false, raw: l } }
  })
  // 传输标记（如 `data: [DONE]`）不是业务事件，单列保存，不参与序号/终态断言。
  const trailers = parsedLines.filter((p) => !p.ok).map((p) => p.raw)
  const events = parsedLines.filter((p) => p.ok).map((p) => p.event)
  events.forEach((e) => eventTypes.push(e.type))
  const complete = events.find((e) => e.type === 'research_complete') || null
  const errors = events.filter((e) => e.type === 'error' || e.type === 'research_cancelled')
  const resolved = events.find((e) => e.type === 'direction_resolved') || null
  // 执行身份取证：事件必须带 run_id / event_seq，终态必须显式标记。
  const runIds = [...new Set(events.map((e) => e.run_id).filter(Boolean))]
  const seqsOk = events.every((e, i) => !e.event_seq || e.event_seq === i + 1)
  const terminals = events.filter((e) => e.terminal)
  const observedRunId = (complete || terminals[terminals.length - 1] || {}).run_id || null
  check('收到终态事件（terminal=true）', terminalSeen && terminals.length >= 1,
        `事件数=${events.length} 终态=${terminals.map((t) => t.type).join(',') || '(无)'}`)
  check('事件带 run_id 且 event_seq 连续（可判别「是不是同一次执行」）',
        runIds.length === 1 && seqsOk, `run_ids=${JSON.stringify(runIds)} seqs_ok=${seqsOk}`)
  check('表单选择的方向真的进了请求体',
        !!requestPayload && requestPayload.direction &&
        requestPayload.direction.choice && requestPayload.direction.choice.preset_id === DIRECTION,
        JSON.stringify(requestPayload && requestPayload.direction))
  // 真实事件形状：profile_hash 在 direction_resolved.content 下（不是顶层）——
  // 断言按**实际事件结构**写，不按期望结构写。
  check('方向解析事件带 profile_hash（表单→请求→状态→Architect）',
        !!resolved && !!resolved.content && !!resolved.content.profile_hash,
        JSON.stringify(resolved && resolved.content))
  check('未出现 error / research_cancelled 事件', errors.length === 0, JSON.stringify(errors).slice(0, 300))
  check('完成事件包含质量状态与哈希（允许 needs_review）',
        !!complete && 'quality_status' in complete && !!complete.report_hash,
        complete ? `quality_status=${complete.quality_status} run_config_hash=${complete.run_config_hash}` : 'no research_complete')

  // ---------- 6. 报告真的渲染进 DOM ----------
  console.log('[6] 报告渲染（真实页面，不是只看网络返回）')
  const report = (complete && complete.final_report) || ''
  await page.waitForTimeout(2000)
  const bodyText = (await page.locator('body').innerText().catch(() => '')) || ''
  // 页面把 markdown 渲染成 DOM（标题/加粗/列表标记都在），所以比较前**两边同样归一化**：
  // 去掉空白与 markdown 记号，只比字符内容。断言仍然要求「报告正文真的进了 DOM」。
  const norm = (s) => s.replace(/[\s#*`>|\-–—_\[\]()]+/g, '')
  const flatReport = norm(report)
  const flatBody = norm(bodyText)
  const span = Math.max(flatReport.length - 60, 0)
  const probes = [0, 0.35, 0.7].map((f) => flatReport.slice(Math.round(f * span), Math.round(f * span) + 60))
                        .filter((p) => p.length >= 40)
  const hit = probes.filter((p) => flatBody.includes(p)).length
  check('最终报告已渲染进页面 DOM',
        !!flatReport && probes.length > 0 && hit === probes.length,
        `报告归一化后 ${flatReport.length} 字；DOM 命中 ${hit}/${probes.length} 段` +
        (probes.length ? `；首段: ${probes[0].slice(0, 40)}` : '；报告为空'))
  await page.screenshot({ path: OUT.replace(/\.json$/, '.png'), fullPage: false }).catch(() => {})

  const elapsed = Math.round((Date.now() - started) / 1000)
  const payload = {
    direction: DIRECTION, query: QUERY, user: user.username, session_id: sessionId,
    years: YEARS,
    elapsed_sec: elapsed,
    // 执行身份取证run_id 与完整事件序列，供「是否同一次执行」判定。
    observed_run_id: observedRunId,
    run_ids: runIds,
    terminal_events: terminals.map((t) => ({ type: t.type, run_id: t.run_id, event_seq: t.event_seq })),
    stream_requests: streamRequests,
    request_payload: requestPayload,
    event_types: eventTypes,
    // **完整事件载荷**（只存信封不能叫完整 SSE 存档）。
    // 业务事件 = 能解析成 JSON 的 data 行；`data: [DONE]` 之类的传输标记单列，
    // **不**混进业务事件数组（否则「序号连续」这类断言会被非业务条目破坏）。
    events,
    transport_trailers: trailers,
    data_event_count: events.length,
    unparsable_count: trailers.length,
    direction_resolved: resolved,
    research_complete: complete ? {
      run_status: complete.run_status, quality_status: complete.quality_status,
      quality_score: complete.quality_score, report_hash: complete.report_hash,
      reviewed_report_hash: complete.reviewed_report_hash,
      run_config_hash: complete.run_config_hash,
      facts_count: complete.facts_count, charts_count: complete.charts_count,
      iterations: complete.iterations, usage: complete.usage,
      references_count: (complete.references || []).length,
    } : null,
    report_chars: report.length,
    // **完整报告**（不再截断）：独立审查要能据此复算报告哈希。
    report_head: report,
    report_preview: report.slice(0, 800),
    dom_report_chars: flatReport.length,
    error_events: errors,
    failed_requests: failedRequests.slice(0, 30),
    console_errors: consoleErrors.slice(0, 20),
    checks: results,
    infos,
  }
  mkdirSync(path.dirname(OUT), { recursive: true })
  writeFileSync(OUT, JSON.stringify(payload, null, 2), 'utf-8')
  console.log(`\n证据已写入: ${OUT}`)
  const passed = results.filter((r) => r.ok).length
  console.log(`\nB 真实纵向联调（表单→报告）: ${passed}/${results.length} PASS，耗时 ${elapsed}s`)
  for (const r of results.filter((x) => !x.ok)) console.log(`  FAILED: ${r.name} — ${r.detail}`)
  await browser.close()
  process.exit(passed === results.length ? 0 : 1)
} catch (err) {
  console.error(`脚本异常: ${err && err.stack ? err.stack : err}`)
  try { await browser.close() } catch (e) { /* ignore */ }
  process.exit(3)
}
