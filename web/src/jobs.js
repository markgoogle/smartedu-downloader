// 全局任务轮询器：单一定时器驱动所有订阅组件（避免每卡片一个 interval）
import { api } from './api.js'

const jobs = new Map()          // id -> job public dict
const listeners = new Set()
let timer = null

function notify() { for (const l of listeners) l(new Map(jobs)) }

async function tick() {
  try {
    const { jobs: list } = await api.jobs()
    jobs.clear()
    for (const j of list) jobs.set(j.id, j)
  } catch { /* 服务暂不可达：保留旧状态 */ }
  notify()
  const active = [...jobs.values()]
    .some(j => j.status === 'queued' || j.status === 'running')
  if (!active && !listeners.size) { clearInterval(timer); timer = null }
}

export function subscribe(cb) {
  listeners.add(cb)
  cb(new Map(jobs))
  if (!timer) { tick(); timer = setInterval(tick, 1200) }
  return () => {
    listeners.delete(cb)
    if (!listeners.size && timer && ![...jobs.values()].some(
      j => j.status === 'queued' || j.status === 'running')) {
      clearInterval(timer); timer = null
    }
  }
}

export async function startDownload(cid, mode = 'auto', pages = null) {
  const { jobId } = await api.startDownload(cid, mode, pages)
  if (!timer) { tick(); timer = setInterval(tick, 1200) }
  return jobId
}

export const cancelJob = api.cancelJob
export { jobs }
