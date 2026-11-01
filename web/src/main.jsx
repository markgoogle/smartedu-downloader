import { useEffect, useRef, useState } from 'preact/hooks'
import { render } from 'preact'
import { api } from './api.js'
import { startDownload } from './jobs.js'
import { JobsPanel, JobBadge, fmtMB } from './components.jsx'
import './style.css'

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const PAGE_SIZE = 24

function toast(msg, kind = '') {
  const el = document.getElementById('toast') || createToast()
  el.textContent = msg
  el.className = 'toast ' + kind
  clearTimeout(el._t)
  el._t = setTimeout(() => el.classList.add('hidden'), 3600)
}
function createToast() {
  const el = document.createElement('div')
  el.id = 'toast'
  document.body.appendChild(el)
  return el
}

// ---------------- 列表页 ----------------
function Browse({ onOpenDetail }) {
  const [q, setQ] = useState('')
  const [filters, setFilters] = useState({ stage: '', subject: '', version: '' })
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const load = async (o = offset, qq = q, ff = filters) => {
    setLoading(true); setErr('')
    try {
      const res = await api.catalog({ q: qq, offset: o, limit: PAGE_SIZE, ...ff })
      setData(res)
    } catch (e) { setErr(e.message) }
    setLoading(false)
  }
  useEffect(() => { load(0, q, filters) }, []) // eslint-disable-line

  const search = () => { setOffset(0); load(0, q, filters) }
  const page = (d) => {
    const no = Math.max(0, Math.min((data.total - PAGE_SIZE), offset + d * PAGE_SIZE))
    setOffset(no); load(no)
  }
  const setF = (k, v) => {
    const nf = { ...filters, [k]: v }
    setFilters(nf); setOffset(0); load(0, q, nf)
  }

  const facetOpts = (dim) => data?.facets?.[dim]
    ? Object.entries(data.facets[dim]).sort((a, b) => b[1] - a[1]).slice(0, 40) : []

  return (
    <section class="panel active">
      <div class="toolbar">
        <input type="search" value={q} placeholder="搜索书名 / 学科 / 册别，如：物理 必修 第三册"
          onInput={e => setQ(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && search()} />
        {['stage', 'subject', 'version'].map(dim => (
          <select value={filters[dim]} onChange={e => setF(dim, e.target.value)}>
            <option value="">{dim === 'stage' ? '学段(全部)' : dim === 'subject' ? '学科(全部)' : '版本(全部)'}</option>
            {facetOpts(dim).map(([v, n]) => <option value={v}>{v} ({n})</option>)}
          </select>
        ))}
        <button class="btn" onClick={search} disabled={loading}>检索</button>
        <span class="muted">
          {data ? `共 ${data.total} 本 / 目录 ${data.catalogSize} 条` : ''}
          {loading ? ' 加载中…' : ''}
        </span>
      </div>
      {err && <div class="warn">{err}
        {' '}（目录为空时请先运行 refresh_catalog.py 导入快照，或点“按 ID 直达”）
        <button class="btn mini ghost" onClick={() => api.reloadCatalog().then(() => load())}>重载目录</button>
      </div>}
      <div class="grid">
        {(data?.items || []).map(it => <BookCard key={it.id} item={it} onOpen={onOpenDetail} />)}
      </div>
      {data && data.total > PAGE_SIZE && (
        <div class="pager">
          <button class="btn ghost" disabled={offset <= 0} onClick={() => page(-1)}>上一页</button>
          <span>{Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(data.total / PAGE_SIZE)}</span>
          <button class="btn ghost" disabled={offset + PAGE_SIZE >= data.total} onClick={() => page(1)}>下一页</button>
        </div>
      )}
    </section>
  )
}

function BookCard({ item, onOpen }) {
  const [busy, setBusy] = useState(false)
  const t = item.tags || {}
  const dl = async (mode) => {
    setBusy(true)
    try {
      const jobId = await startDownload(item.id, mode)
      toast(`已加入下载任务（${jobId}），可在“下载任务”查看`)
    } catch (e) { toast('启动失败：' + e.message, 'err') }
    setBusy(false)
  }
  return (
    <div class="tcard">
      <div class="cover">📘</div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <h4 title={item.title}>{item.title}</h4>
        <div class="chips">
          {[t.stage, t.subject, t.grade, t.volume, t.version].filter(Boolean)
            .map(v => <span class="chip">{v}</span>)}
        </div>
        <div class="mono">{item.id}</div>
        <div class="row">
          <button class="btn mini" disabled={busy} onClick={() => dl('auto')}>⬇ 一键下载</button>
          <button class="btn mini ghost" onClick={() => onOpen(item.id)}>详情/选页</button>
        </div>
      </div>
    </div>
  )
}

// ---------------- 详情弹窗 ----------------
function DetailModal({ cid, onClose }) {
  const [meta, setMeta] = useState(null)
  const [err, setErr] = useState('')
  const [pages, setPages] = useState('')
  useEffect(() => {
    setMeta(null); setErr('')
    api.detail(cid).then(setMeta).catch(e => setErr(e.message))
  }, [cid])

  const dl = async (mode) => {
    try {
      const p = pages.trim() || null
      if (p && !/^\d+-\d+$/.test(p)) return toast('页码范围格式：起始-结束', 'err')
      const jobId = await startDownload(cid, mode, p)
      toast(`任务 ${jobId} 已启动`)
      onClose()
    } catch (e) { toast('启动失败：' + e.message, 'err') }
  }

  return (
    <div class="modal" onClick={e => e.target === e.currentTarget && onClose()}>
      <div class="sheet">
        <button class="close" onClick={onClose}>✕</button>
        {err && <div class="warn">{err}</div>}
        {!meta && !err && <p class="muted">读取公开详情 JSON…</p>}
        {meta && (
          <div>
            <h3>{meta.title}</h3>
            <div class="chips">
              {[meta.stage, meta.subject, meta.grade, meta.volume, meta.publisher]
                .filter(Boolean).map(v => <span class="chip">{v}</span>)}
            </div>
            <table class="kv">
              <tr><td>contentId</td><td class="mono">{meta.contentId}</td></tr>
              <tr><td>页数（权威 pagesize）</td><td>{meta.pagesize}</td></tr>
              <tr><td>preview 条目数</td><td>{meta.images.previewCount}（≠页数）</td></tr>
              <tr><td>原生 PDF 声明字节</td><td>{fmtMB(meta.native.size)}</td></tr>
              <tr><td>图片版声明总字节</td><td>{fmtMB(meta.images.totalSize)}</td></tr>
              <tr><td>路径A命中预测</td><td>
                {meta.native.predictHit == null ? '无法预测（文件名无时间戳，实测为准）'
                  : meta.native.predictHit ? '预计可命中（≥2026-08 批次）'
                  : '预计 403（将自动回退路径B）'}
              </td></tr>
            </table>
            <div class="row">
              <button class="btn" onClick={() => dl('auto')}>一键下载（A→B 自动回退）</button>
              <button class="btn ghost" onClick={() => dl('native')}>仅路径A</button>
              <button class="btn ghost" onClick={() => dl('images')}>仅路径B</button>
            </div>
            <div class="row">
              <input style={{ width: 140 }} placeholder="选页：如 1-5（仅路径B）"
                value={pages} onInput={e => setPages(e.target.value)} />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------- 直达 ----------------
function Direct({ onOpenDetail }) {
  const [cid, setCid] = useState('')
  const go = () => {
    const v = cid.trim().toLowerCase()
    if (!UUID_RE.test(v)) return toast('contentId 需为标准 UUID', 'err')
    onOpenDetail(v)
  }
  return (
    <section class="panel active">
      <div class="card narrow">
        <h3>用 contentId 直达</h3>
        <p class="muted">粘贴平台教材的 contentId（UUID），直接读取公开详情 JSON（免登录）。</p>
        <div class="row">
          <input value={cid} onInput={e => setCid(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && go()}
            placeholder="例如 b2774427-227a-496b-805e-59b510c87ebe" />
          <button class="btn" onClick={go}>查询</button>
        </div>
      </div>
    </section>
  )
}

// ---------------- 说明 ----------------
function Help() {
  return (
    <section class="panel active">
      <div class="card">
        <h3>工作流程（与 REPORT.md 实测一致，2026-11-01）</h3>
        <ol class="flow">
          <li><b>详情 JSON</b>：<code>s-file-{'{1,2,3}'}.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/&#123;contentId&#125;.json</code> —— 公开、免登录、免 Token、免 Referer。</li>
          <li><b>路径 A（原生 PDF）</b>：<code>ti_items[source].ti_storages</code> 私有域直链去掉 <code>-private</code> → 公开桶；约 1/3 命中，其余 403 AccessDenied。</li>
          <li><b>路径 B（图片合成）</b>：preview URL 替换页码 <code>/&#123;page&#125;.jpg</code>，页数取 <code>requirements.pagesize</code>（≠ preview 条目数）；原始 JPEG 以 /DCTDecode 零重编码合成。</li>
          <li><b>校验</b>：字节数==ti_size、%PDF- 魔数、%%EOF 尾部、页数==pagesize、pagesize+1 页应 403。</li>
        </ol>
        <h3>注意</h3>
        <ul>
          <li>全站请求<b>不发送 Referer</b>（带非白名单 Referer 会 403）。</li>
          <li>列表数据来自本地快照 <code>catalog_data/*.json</code>（平台目录端点匿名不可达），可用 <code>refresh_catalog.py</code> 刷新。</li>
          <li>产物保存在 <code>downloads/&lt;contentId&gt;/</code>，完成任务 24h 后自动回收。</li>
          <li>安全：出站 URL 走域名白名单（防 SSRF）、任务上限与限流、静态资源 CSP/no-referrer。</li>
        </ul>
      </div>
    </section>
  )
}

// ---------------- 目录抓取（Cookie 登录态可选） ----------------
const PHASE_TEXT = { idle: '空闲', search: '列表接口枚举中', seeds: '种子扩散中',
                     random: '随机采样发现中', done: '完成', stopped: '已手动停止',
                     error: '出错' }
const SEARCH_TEXT = { '': '', skipped: '未配置 Cookie，跳过列表通道',
                      ok: '列表接口可用 ✓', unauthorized: 'Cookie 无效/过期（403/401）',
                      unreachable: '列表接口不可达（网关拦截）',
                      badresponse: '列表响应格式未知' }

function CrawlPanel({ onDone }) {
  const [sess, setSess] = useState(null)
  const [cookie, setCookie] = useState('')
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.session().then(setSess).catch(() => {})
    return () => { clearInterval(pollRef.current); pollRef.current = null }
  }, [])

  const pollRef = useRef(null)
  const watch = () => {
    clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const s = await api.crawlStatus()
        setStatus(s)
        if (!s.running) {
          clearInterval(pollRef.current); pollRef.current = null
          api.session().then(setSess).catch(() => {})
          if (s.phase === 'done' || s.phase === 'stopped') {
            await api.reloadCatalog().catch(() => {})
            onDone && onDone()
            toast(`抓取结束：新增 ${s.hits} 本`, 'ok')
          }
        }
      } catch { /* 忽略瞬时错误 */ }
    }, 2000)
  }

  const saveCookie = async () => {
    setBusy(true); setErr('')
    try {
      setSess(await api.setSession(cookie.trim()))
      setCookie('')
      toast('Cookie 已保存到内存（不落盘、不回显）', 'ok')
    } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const clearCookie = async () => {
    setBusy(true)
    try { setSess(await api.clearSession()); toast('已清除 Cookie') }
    catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const start = async () => {
    setBusy(true); setErr('')
    try { await api.crawlStart({}); watch(); setStatus(await api.crawlStatus()) }
    catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const stop = async () => {
    setBusy(true)
    try { await api.crawlStop() } catch (e) { setErr(e.message) }
    setBusy(false)
  }

  const running = status?.running
  return (
    <section class="panel active">
      <div class="card narrow">
        <h3>自动抓取全站教材目录</h3>
        <p class="muted">
          通道①「列表接口」：<code>x-search.ykt.cbern.com.cn</code> 聚合检索——页面公开，但该接口需登录态；
          在下方粘贴浏览器 <b>完整 Cookie</b>（F12 → Network → 任意 smartedu 请求 → Request Headers → Cookie）即可解锁，
          可分页枚举全站教材。Cookie 仅存服务端内存，绝不落盘/写日志/回显。<br />
          通道②「匿名保底」：无 Cookie 或接口被网关拦截时，自动走 种子扩散 + UUID 采样（公开详情 JSON）。
        </p>

        {sess?.configured
          ? <div class="chips"><span class="chip">已配置 Cookie：{sess.count} 项（{sess.names.slice(0, 4).join(', ')}…）</span></div>
          : <div class="warn small">未配置 Cookie —— 将只走匿名保底通道。</div>}
        <div class="row">
          <input style={{ flex: 1 }} type="text" spellcheck={false}
            placeholder="粘贴平台登录后的完整 Cookie 串（k1=v1; k2=v2; …）"
            value={cookie} onInput={e => setCookie(e.target.value)} />
          <button class="btn mini" disabled={busy || !cookie.trim()} onClick={saveCookie}>保存</button>
          {sess?.configured && <button class="btn mini ghost" disabled={busy} onClick={clearCookie}>清除</button>}
        </div>
        {err && <div class="st-err small">{err}</div>}

        <div class="row">
          {!running
            ? <button class="btn" disabled={busy} onClick={start}>▶ 开始抓取</button>
            : <button class="btn ghost" onClick={stop}>■ 停止</button>}
          {status && <span class="muted">
            {PHASE_TEXT[status.phase] || status.phase}
            {' · '}请求 {status.requests} / 新增 {status.hits} / 已有 {status.known}
            {status.elapsed ? ` · ${Math.round(status.elapsed)}s` : ''}
          </span>}
        </div>
        {status && (
          <table class="kv">
            <tr><td>列表通道状态</td><td>{SEARCH_TEXT[status.searchStatus] || status.searchStatus || '—'}</td></tr>
            <tr><td>最近命中</td><td>{status.lastHitTitle || '—'}</td></tr>
            {status.error && <tr><td>错误</td><td class="st-err">{status.error}</td></tr>}
          </table>
        )}
        {running && <ProgressBar job={{ status: 'running', downloaded: status?.hits || 0, total: 0 }} />}
      </div>
    </section>
  )
}

// ---------------- App ----------------
function App() {
  const [tab, setTab] = useState('browse')
  const [detailCid, setDetailCid] = useState(null)
  const [health, setHealth] = useState(null)
  useEffect(() => { api.health().then(setHealth).catch(() => {}) }, [])

  return (
    <div>
      <header class="topbar">
        <div class="brand"><span class="logo">📚</span>
          <div><b>国家中小学智慧教育平台 · 教材下载台</b>
            <small>免登录 · 原生PDF优先，自动回退图片合成
              {health ? ` · 目录 ${health.catalog.entries} 条` : ' · 连接中…'}</small></div>
        </div>
        <nav class="tabs">
          <button class={tab === 'browse' ? 'active' : ''} onClick={() => setTab('browse')}>教材列表</button>
          <button class={tab === 'crawl' ? 'active' : ''} onClick={() => setTab('crawl')}>抓取目录</button>
          <button class={tab === 'direct' ? 'active' : ''} onClick={() => setTab('direct')}>按 ID 直达</button>
          <button class={tab === 'jobs' ? 'active' : ''} onClick={() => setTab('jobs')}>
            下载任务 <JobBadge /></button>
          <button class={tab === 'help' ? 'active' : ''} onClick={() => setTab('help')}>说明</button>
        </nav>
      </header>
      <main>
        {tab === 'browse' && <Browse onOpenDetail={setDetailCid} />}
        {tab === 'crawl' && <CrawlPanel onDone={() => setTab('browse')} />}
        {tab === 'direct' && <Direct onOpenDetail={setDetailCid} />}
        {tab === 'jobs' && <JobsPanel />}
        {tab === 'help' && <Help />}
      </main>
      {detailCid && <DetailModal cid={detailCid} onClose={() => setDetailCid(null)} />}
    </div>
  )
}

render(<App />, document.getElementById('app'))
