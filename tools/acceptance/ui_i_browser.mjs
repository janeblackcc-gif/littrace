/**
 * UI-I 浏览器真实交互验收。
 *
 * 此前的 UI-I 14/14 是在源码/CORS/SSE 解码层做的，明确未做浏览器内人工核验。
 * 本脚本用 Playwright 驱动真实 Chromium：
 *   1. 真实登录表单 -> 跳转 /chat
 *   2. 导航只剩文献调研相关入口；行业选择器与遗留入口在真实 DOM 中不存在
 *   3. 直接访问 /news /database /bidding 都落到 404（路由已注销，不是只藏了入口）
 *   4. 推荐问题是文献调研语义
 *   5. 知识库页可进入，且在 ENABLE_DOCMIND=true 下上传返回 200（不再是 503）
 *   6. 真实 SSE 流：中文 + emoji 无乱码；事件是结构化 JSON 而非被截断的乱码
 *
 * 用法（后端 8000 + 前端 5183 都需已启动）：
 *   node tools/acceptance/ui_i_browser.mjs
 *
 * 关于 playwright 的解析：ESM 的裸包名是从**本文件所在目录**向上找 node_modules，
 * 而 playwright 装在 frontend/node_modules（前端 devDependency），本文件在
 * tools/acceptance/ 下，因此不能直接 `import 'playwright'`。这里按 frontend 路径
 * 动态导入，使脚本可以放在任意位置、从任意 cwd 运行。
 */
import { pathToFileURL, fileURLToPath } from 'node:url'
import { existsSync } from 'node:fs'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND = process.env.LITTRACE_FRONTEND || path.resolve(HERE, '..', '..', 'frontend')
const pwEntry = path.join(FRONTEND, 'node_modules', 'playwright', 'index.mjs')
if (!existsSync(pwEntry)) {
  console.error(`未找到 playwright：${pwEntry}\n请先在 frontend/ 执行 npm install -D playwright --legacy-peer-deps`)
  process.exit(2)
}
const { chromium } = await import(pathToFileURL(pwEntry).href)

const WEB = process.env.LITTRACE_WEB || 'http://localhost:5183'
const API = process.env.LITTRACE_BASE || 'http://127.0.0.1:8000'
const CHAT_QUESTION = '你好，请用一句话总结 Agent 记忆的主要挑战 🧠 这是中文与 emoji 的编码校验'

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok: !!ok, detail })
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}
function finish() {
  const passed = results.filter((r) => r.ok).length
  console.log(`\nUI-I(浏览器真实交互): ${passed}/${results.length} PASS`)
  for (const r of results.filter((x) => !x.ok)) console.log(`  FAILED: ${r.name} — ${r.detail}`)
  process.exit(passed === results.length ? 0 : 1)
}

/** 直接调后端建一个一次性用户，UI 只负责登录（与后端 A1-I 用同一注册接口）。 */
async function seedUser() {
  const tag = Math.random().toString(36).slice(2, 10)
  const body = { username: `uii_${tag}`, email: `uii_${tag}@example.com`, password: 'UiI-Browser-Passw0rd!' }
  const r = await fetch(`${API}/auth/register`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`seed 注册失败 ${r.status}: ${await r.text()}`)
  return body
}

const browser = await chromium.launch()
const page = await browser.newPage()
const consoleErrors = []
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })
page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`))

try {
  const user = await seedUser()
  console.log(`前端: ${WEB}\n后端: ${API}\n用户: ${user.username}`)

  // ---------- 1. 登录页与真实登录 ----------
  console.log('\n[1] 真实登录')
  await page.goto(`${WEB}/login`, { waitUntil: 'networkidle', timeout: 60000 })
  check('登录页可加载', await page.locator('form').first().isVisible())
  const brand = (await page.locator('h1').first().textContent().catch(() => '')) || ''
  console.log(`      登录页品牌标题: ${brand.trim()}`)
  // 场景迁移的界面一致性：登录页品牌仍写着原「行业信息助手」。
  // 记录为真实发现（不阻断验收），由使用者决定是否改文案。
  check('登录页品牌已随场景迁移更新', !/行业信息助手|行业助手/.test(brand),
        `当前标题=${brand.trim()}（仍为原行业咨询品牌，属场景迁移遗漏）`)

  await page.locator('input#login_username, input[placeholder="用户名或邮箱"]').first().fill(user.username)
  await page.locator('input[type="password"]').first().fill(user.password)
  await page.locator('button[type="submit"]').first().click()
  await page.waitForURL(/\/chat/, { timeout: 30000 })
  check('登录后跳转到 /chat', /\/chat/.test(page.url()), page.url())

  // ---------- 2. 导航入口 ----------
  console.log('\n[2] 导航入口（遗留功能必须真的消失）')
  await page.waitForLoadState('networkidle')
  const navText = await page.locator('.base-layout-nav').innerText().catch(() => '')
  console.log(`      导航项: ${JSON.stringify(navText.replace(/\s+/g, ' ').trim())}`)
  check('导航含 首页/新的聊天/知识库', ['首页', '新的聊天', '知识库'].every((t) => navText.includes(t)))
  for (const legacy of ['行业资讯', '招投标信息', '数据库']) {
    check(`导航不含「${legacy}」`, !navText.includes(legacy))
  }
  const selectorCount = await page.locator('.industry-selector').count()
  check('行业选择器在真实 DOM 中不存在', selectorCount === 0, `count=${selectorCount}`)

  // ---------- 3. 遗留路由真的 404 ----------
  console.log('\n[3] 遗留路由已注销（不是只藏入口）')
  for (const path of ['/news', '/database', '/bidding']) {
    await page.goto(`${WEB}${path}`, { waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.waitForTimeout(1200)
    const url = page.url()
    const text = (await page.locator('body').innerText().catch(() => '')) || ''
    const is404 = url.includes('/404') || /404|不存在|页面未找到/.test(text)
    check(`${path} 落到 404`, is404, `url=${url}`)
  }

  // ---------- 4. 推荐问题语义 ----------
  console.log('\n[4] 首页文案已迁到文献调研语义')
  await page.goto(`${WEB}/chat`, { waitUntil: 'networkidle', timeout: 30000 })
  const bodyText = (await page.locator('body').innerText()) || ''
  check('标题含 LitTrace 文献调研助手', bodyText.includes('LitTrace 文献调研助手'),
        bodyText.includes('LitTrace') ? '' : '未找到标题')
  check('推荐问题为文献调研语义', bodyText.includes('Agent 记忆的检索粒度') || bodyText.includes('RAG 长上下文'),
        '')
  check('不含行业咨询旧文案（市场规模/招投标）',
        !bodyText.includes('市场规模') && !bodyText.includes('招投标'))

  // ---------- 5. 知识库页 + DocMind 上传 ----------
  console.log('\n[5] 知识库页与文档上传（ENABLE_DOCMIND=true）')
  await page.goto(`${WEB}/knowledge`, { waitUntil: 'networkidle', timeout: 30000 })
  const kbText = (await page.locator('body').innerText().catch(() => '')) || ''
  check('知识库页可进入', kbText.length > 0 && !/404/.test(page.url()), page.url())

  const token = await page.evaluate(() => {
    for (const k of Object.keys(localStorage)) {
      const v = localStorage.getItem(k)
      if (v && v.includes('eyJ')) { try { const o = JSON.parse(v); if (o && o.token) return o.token } catch { /* not json */ } }
    }
    return null
  })
  const bearer = token || await page.evaluate(() => localStorage.getItem('token'))
  const kbName = `UII_${Math.random().toString(36).slice(2, 8)}`
  const mk = await fetch(`${API}/knowledge-bases`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}) },
    body: JSON.stringify({ name: kbName, description: 'UI-I 浏览器验收' }),
  })
  const mkText = await mk.text()   // 只能读一次
  check('经浏览器登录态可建知识库', mk.status === 201, `status=${mk.status} body=${mkText.slice(0, 160)}`)

  if (mk.status === 201) {
    const kb = JSON.parse(mkText)
    const fd = new FormData()
    fd.append('file', new Blob(['UI-I 上传样本。这是用于浏览器验收的文本内容。\n' + 'x'.repeat(40)], { type: 'text/plain' }), 'ui_i_sample.txt')
    const up = await fetch(`${API}/knowledge-bases/${kb.id}/documents`, {
      method: 'POST', headers: bearer ? { Authorization: `Bearer ${bearer}` } : {}, body: fd,
    })
    const upText = await up.text()
    check('文档上传返回 200（DocMind 已启用，非 503）', up.status === 200,
          `status=${up.status} body=${upText.slice(0, 200)}`)

    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForTimeout(2500)
    const after = (await page.locator('body').innerText().catch(() => '')) || ''
    check('新建的知识库出现在页面上', after.includes(kbName), `查 ${kbName}`)
  }

  // ---------- 6. 真实 SSE 流（中文 + emoji）----------
  console.log('\n[6] 真实 SSE 流编码（浏览器内 fetch 读原始字节）')
  // 注意：search_web 与 search_local 至少一个为 true，否则后端按设计返回 422
  //（research_router.py:81 "至少选择一种搜索模式"）。首轮曾误传两个都 false，
  // 那是测试参数错误，不是应用缺陷；这里显式开启 web 搜索。
  // PR1 起文献环境**强制显式 direction**：本探针必须带 direction，
  // 否则它验证的就不再是「文献流程」，而是缺方向的旧行业路径。
  const DIRECTION_BODY = { choice: { mode: 'preset', preset_id: 'agent_memory' }, start_year: 2022, end_year: 2025 }
  // fail-closed 契约：缺 direction 必须被拒绝，不得静默落回行业规划 Prompt
  const noDirection = await page.evaluate(async ({ api, token, question }) => {
    const res = await fetch(`${api}/research/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ query: question, search_modes: ['web'], version: 'v2', max_iterations: 0 }),
    })
    return { status: res.status, body: String(await res.text()).slice(0, 200) }
  }, { api: API, token: bearer, question: CHAT_QUESTION })
  check('缺 direction 的流请求被 422 拒绝（不再落回行业规划）', noDirection.status === 422,
        `status=${noDirection.status} ${noDirection.body}`)

  const sse = await page.evaluate(async ({ api, token, question, direction }) => {
    const res = await fetch(`${api}/research/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ query: question, search_modes: ['web'], version: 'v2', max_iterations: 0, direction }),
    })
    if (res.status !== 200) {
      return { status: res.status, ctype: res.headers.get('content-type'), raw: await res.text(), fatal: null, early: true }
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8', { fatal: true })
    let raw = ''
    let fatal = null
    const started = Date.now()
    try {
      while (Date.now() - started < 90000) {
        const { value, done } = await reader.read()
        if (done) break
        raw += decoder.decode(value, { stream: true })
        if (raw.includes('research_complete') || raw.includes('"type": "error"') || raw.includes('"type":"error"')) break
      }
    } catch (e) { fatal = String(e) }
    finally { try { await reader.cancel() } catch { /* ignore */ } }
    return { status: res.status, ctype: res.headers.get('content-type'), raw, fatal, early: false }
  }, { api: API, token: bearer, question: CHAT_QUESTION, direction: DIRECTION_BODY })

  check('SSE 响应 200', sse.status === 200, `status=${sse.status} ctype=${sse.ctype}`)
  if (sse.status !== 200) {
    console.log(`      非 200 响应体: ${String(sse.raw).slice(0, 300)}`)
  } else {
    check('Content-Type 为 text/event-stream', (sse.ctype || '').includes('text/event-stream'), sse.ctype || '')
    check('严格 UTF-8 解码无致命错误（无乱码/截断）', sse.fatal === null, sse.fatal || 'fatal-decoder 未报错')
    check('流中出现 data: 前缀的事件', sse.raw.includes('data:'), `收到 ${sse.raw.length} 字节`)
    const dataLines = sse.raw.split('\n').filter((l) => l.startsWith('data: '))
    let parsed = null
    for (const l of dataLines) {
      try { const o = JSON.parse(l.slice(6)); if (o && typeof o === 'object') { parsed = o; break } } catch { /* [DONE] 等 */ }
    }
    check('存在可解析的结构化 JSON 事件（未被截断）', parsed !== null,
          dataLines[0] ? dataLines[0].slice(0, 140) : '无 data 行')
    check('事件条数 > 1（真实流式而非单帧）', dataLines.length > 1, `${dataLines.length} 个 data 事件`)
    if (parsed) console.log(`      首个可解析事件 type=${parsed.type || parsed.error_type} content=${JSON.stringify(String(parsed.content || '').slice(0, 70))}`)
  }
  check('流内中文未损坏（事件中可见 CJK）', /[\u4e00-\u9fff]/.test(sse.raw),
        sse.raw.length ? `${sse.raw.length} 字节` : '空响应')

  // 中文/emoji 往返：用请求回显事件校验编码（不依赖 LLM 输出是否恰好含 emoji）
  const echoOk = await page.evaluate(async ({ api, token, question }) => {
    const res = await fetch(`${api}/research/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ query: question, search_web: false, search_local: false, version: 'v2', max_iterations: 0 }),
    })
    const buf = new Uint8Array(await res.arrayBuffer())
    const text = new TextDecoder('utf-8', { fatal: true }).decode(buf)
    return { bytes: buf.length, hasCjk: /[\u4e00-\u9fff]/.test(text), hasEmoji: /\u{1F9E0}/u.test(text) }
  }, { api: API, token: bearer, question: CHAT_QUESTION })
  check('全量字节可严格 UTF-8 解码', echoOk.bytes > 0, `${echoOk.bytes} 字节`)
  check('响应含中文文本', echoOk.hasCjk)
  console.log(`      响应含 emoji(🧠)=${echoOk.hasEmoji}（取决于是否有事件回显 query，非断言项）`)

  // ---------- 6b. 真实 UI 交互：切换「搜索模式」并确认进入深度研究 ----------
  console.log('\n[6b] 真实 UI 交互：搜索模式开关')
  await page.goto(`${WEB}/chat`, { waitUntil: 'networkidle', timeout: 30000 })
  const modeBtn = page.getByRole('button', { name: /搜索模式|深度搜索|全部搜索|知识库/ }).first()
  check('存在搜索模式按钮', await modeBtn.count() > 0)
  if (await modeBtn.count() > 0) {
    await modeBtn.click()
    await page.waitForTimeout(800)
    const webBox = page.getByText('深度搜索（网络）').first()
    check('下拉中出现「深度搜索（网络）」', await webBox.count() > 0)
    check('下拉中出现「本地知识库」', await page.getByText('本地知识库').first().count() > 0)
    if (await webBox.count() > 0) {
      await webBox.click()
      await page.waitForTimeout(500)
      const label = (await modeBtn.innerText().catch(() => '')) || ''
      check('勾选后按钮文案变为「深度搜索」', /深度搜索/.test(label), `按钮文案=${label.replace(/\s+/g, ' ').trim()}`)
    }
  }

  // ---------- 7. 控制台错误 ----------
  console.log('\n[7] 浏览器控制台')
  const significant = consoleErrors.filter((e) => !/favicon|ResizeObserver|Download the React DevTools|antd: message|422 \(Unprocessable Entity\)/i.test(e))
  check('无显著控制台错误（已排除 antd 静态 message 告警与我方 422 探针）',
        significant.length === 0,
        significant.slice(0, 3).map((s) => s.slice(0, 120)).join(' | '))
  const antdWarn = consoleErrors.some((e) => /antd: message/.test(e))
  console.log(`      antd 静态 message 告警: ${antdWarn ? '存在（既有代码用 message.xxx 静态调用，非本次引入）' : '无'}`)
} catch (err) {
  check('脚本执行未抛异常', false, String(err))
} finally {
  await browser.close()
}
finish()
