// 轻量 API 封装：同源、无凭据（本站无需登录）、错误统一抛出可读信息
const j = async (url, opts = {}) => {
  const res = await fetch(url, {
    credentials: 'same-origin',
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    ...opts,
  })
  let data = null
  try { data = await res.json() } catch { /* ignore */ }
  if (!res.ok) {
    const msg = (data && (data.error || data.detail)) || `HTTP ${res.status}`
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    err.status = res.status
    throw err
  }
  return data
}

export const api = {
  health: () => j('/api/health'),
  catalog: ({ q = '', offset = 0, limit = 24, ...filters }) => {
    const p = new URLSearchParams({ q, offset, limit })
    for (const [k, v] of Object.entries(filters)) if (v) p.set(k, v)
    return j(`/api/catalog?${p}`)
  },
  reloadCatalog: () => j('/api/catalog/reload', { method: 'POST' }),
  detail: (cid) => j(`/api/detail?contentId=${encodeURIComponent(cid)}`),
  startDownload: (contentId, mode = 'auto', pages = null) =>
    j('/api/download', { method: 'POST', body: JSON.stringify(
      { contentId, mode, ...(pages ? { pages } : {}) }) }),
  job: (id) => j(`/api/job?id=${encodeURIComponent(id)}`),
  jobs: () => j('/api/jobs'),
  cancelJob: (id) => j(`/api/job/cancel?id=${encodeURIComponent(id)}`, { method: 'POST' }),
  fileUrl: (jobId, inline = false) =>
    `/api/file?job=${encodeURIComponent(jobId)}${inline ? '&inline=1' : ''}`,
}
