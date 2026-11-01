import { useEffect, useState } from 'preact/hooks'
import { subscribe, startDownload, cancelJob } from './jobs.js'

export const fmtMB = (b) => b == null ? '-' : (b / 1048576).toFixed(1) + ' MB'

export function JobBadge() {
  const [snap, setSnap] = useState(new Map())
  useEffect(() => subscribe(setSnap), [])
  const n = [...snap.values()].filter(j => j.status === 'queued' || j.status === 'running').length
  return <i class="badge">{n}</i>
}

export function ProgressBar({ job }) {
  let pct = 0
  if (job.status === 'done') pct = 100
  else if (job.total > 0) pct = Math.min(99, Math.round(job.downloaded * 100 / job.total))
  return <div class="bar"><i style={{ width: pct + '%' }}></i></div>
}

const stCls = { running: 'st-run', queued: 'st-run', done: 'st-done',
                 error: 'st-err', cancelled: 'st-err' }
const stText = { running: '进行中', queued: '排队中', done: '完成',
                 error: '失败', cancelled: '已取消' }

export function JobCard({ job, onCancel = cancelJob }) {
  const r = job.result
  return (
    <div class="job">
      <div class="row" style={{ justifyContent: 'space-between' }}>
        <b>{r?.title || job.cid}</b>
        <span class={stCls[job.status] || ''}>{stText[job.status] || job.status}
          · {job.mode}{r?.fallbackUsed ? '（A→B 回退）' : ''}</span>
      </div>
      <ProgressBar job={job} />
      <div class="muted">{job.step}
        {job.total > 0 ? ` · ${job.downloaded}/${job.total}` : ''}
        {job.bytes && job.status !== 'done' ? ` · ${fmtMB(job.bytes)}` : ''}
        {r?.bytes ? ` · ${fmtMB(r.bytes)}` : ''}
        {r?.pages ? ` · ${r.pages} 页` : ''}
        {r?.mode === 'native' && r.declaredBytes != null ? ` · 字节校验${r.declaredBytes === r.bytes ? '一致✓' : '不一致✗'}` : ''}
        {r?.boundaryStatus ? ` · 越界页HTTP ${r.boundaryStatus}${r.boundaryStatus === 403 ? '✓' : ''}` : ''}
      </div>
      {job.error && <div class="st-err small">{job.error}</div>}
      <div class="row">
        {r && <a class="btn mini" href={`/api/file?job=${job.id}`} rel="noopener">⬇ 下载 PDF</a>}
        {r && <a class="btn mini ghost" href={`/api/file?job=${job.id}&inline=1`}
                  target="_blank" rel="noopener noreferrer">在线预览</a>}
        {(job.status === 'running' || job.status === 'queued') &&
          <button class="btn mini ghost" onClick={() => onCancel(job.id)}>取消</button>}
      </div>
      {job.log?.length > 0 && <details class="logline"><summary>日志</summary>
        {job.log.map((l, i) => <div key={i}>{l}</div>)}</details>}
    </div>
  )
}

export function JobsPanel() {
  const [snap, setSnap] = useState(new Map())
  useEffect(() => subscribe(setSnap), [])
  const list = [...snap.values()]
  if (!list.length) return <p class="muted">暂无任务。在列表或“直达”里点“一键下载”即可。</p>
  return <div class="list">{list.map(j => <JobCard key={j.id} job={j} />)}</div>
}
