// Backtrack Desktop pane: a timeline of the agent's file-changing actions with preview and restore.
// Plain ESM, no build step. Only @hermes/plugin-sdk, react and react/jsx-runtime are imported.
import { PANES_AREA } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useEffect, useState } from 'react'

const h = (type, props, ...children) =>
  children.length <= 1
    ? jsx(type, { ...props, children: children[0] })
    : jsxs(type, { ...props, children })

const css = {
  wrap: { display: 'flex', flexDirection: 'column', height: '100%', fontSize: 12, gap: 8, padding: 10, overflow: 'auto' },
  head: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  muted: { opacity: 0.65 },
  row: { border: '1px solid rgba(128,128,128,.3)', borderRadius: 6, padding: 8, display: 'flex', flexDirection: 'column', gap: 4 },
  rowTop: { display: 'flex', justifyContent: 'space-between', gap: 8 },
  badge: { fontFamily: 'monospace', padding: '0 6px', borderRadius: 4, background: 'rgba(128,128,128,.2)' },
  summary: { fontFamily: 'monospace', wordBreak: 'break-all' },
  btns: { display: 'flex', gap: 6, flexWrap: 'wrap' },
  btn: { cursor: 'pointer', padding: '2px 8px', borderRadius: 4, border: '1px solid rgba(128,128,128,.5)', background: 'transparent', color: 'inherit' },
  danger: { cursor: 'pointer', padding: '2px 8px', borderRadius: 4, border: '1px solid #c0392b', background: '#c0392b', color: '#fff' },
  diff: { fontFamily: 'monospace', whiteSpace: 'pre-wrap', maxHeight: 220, overflow: 'auto', background: 'rgba(128,128,128,.12)', padding: 6, borderRadius: 4 },
  err: { color: '#c0392b' },
  ok: { color: '#27ae60' },
}

function timeAgo(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

async function call(ctx, path, opts) {
  let res = await ctx.rest(path, opts)
  if (res && typeof res.json === 'function') res = await res.json()
  return res
}

function Row({ a, ctx, reload, root }) {
  const [diff, setDiff] = useState(null)
  const [confirm, setConfirm] = useState(false)
  const [msg, setMsg] = useState(null)

  const preview = async () => {
    setMsg(null)
    const r = await call(ctx, `/diff?id=${a.id}${root ? `&root=${encodeURIComponent(root)}` : ''}`)
    setDiff(r.ok ? r : { error: r.error })
  }
  const restore = async () => {
    const r = await call(ctx, '/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: a.id, root }),
    })
    setConfirm(false)
    setDiff(null)
    setMsg(r.ok
      ? { ok: true, text: `Restored: ${r.written} written, ${r.removed} removed, ${r.verified ? 'verified' : 'NOT verified'}` }
      : { ok: false, text: r.error })
    reload()
  }

  const files = a.files || []
  return h('div', { style: css.row },
    h('div', { style: css.rowTop },
      h('span', null, h('span', { style: css.badge }, `#${a.id} ${a.tool}`)),
      h('span', { style: css.muted }, timeAgo(a.ts))),
    h('div', { style: css.summary }, a.summary),
    h('div', { style: css.muted },
      a.before ? `${files.length} file(s) changed` : `not checkpointed: ${a.skipped || 'unknown reason'}`,
      a.reverted_by ? ' - reverted' : ''),
    a.undoable
      ? h('div', { style: css.btns },
          h('button', { style: css.btn, onClick: preview }, 'Preview'),
          confirm
            ? h('button', { style: css.danger, onClick: restore }, 'Confirm restore')
            : h('button', { style: css.btn, onClick: () => setConfirm(true) }, 'Restore to before'))
      : null,
    msg ? h('div', { style: msg.ok ? css.ok : css.err }, msg.text) : null,
    diff && diff.error ? h('div', { style: css.err }, diff.error) : null,
    diff && !diff.error
      ? h('pre', { style: css.diff }, diff.changes.length ? diff.diff : 'Files already match this checkpoint.')
      : null)
}

function Pane({ ctx }) {
  const [state, setState] = useState({ loading: true })
  const reload = useCallback(async () => {
    try {
      const r = await call(ctx, '/timeline?limit=50')
      setState(r.ok ? { data: r } : { error: r.error || 'backend returned an error' })
    } catch (e) {
      setState({ error: `Backtrack backend unreachable: ${e && e.message ? e.message : e}` })
    }
  }, [ctx])

  useEffect(() => {
    reload()
    const t = setInterval(reload, 3000)
    return () => clearInterval(t)
  }, [reload])

  const redo = async () => {
    await call(ctx, '/redo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    reload()
  }

  let body
  if (state.loading) body = h('div', { style: css.muted }, 'Loading...')
  else if (state.error) body = h('div', { style: css.err }, state.error)
  else if (state.data.empty || !state.data.actions.length)
    body = h('div', { style: css.muted }, 'No file-changing actions checkpointed yet. Ask Hermes to change something.')
  else {
    const actions = state.data.actions.slice().reverse()
    body = h('div', { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
      ...actions.map((a) => h(Row, { key: a.id, a, ctx, reload, root: state.data.root })))
  }

  return h('div', { style: css.wrap },
    h('div', { style: css.head },
      h('strong', null, 'Backtrack'),
      h('button', { style: css.btn, onClick: redo }, 'Redo last undo')),
    state.data && state.data.root ? h('div', { style: css.muted }, state.data.root) : null,
    body)
}

export default {
  id: 'backtrack',
  name: 'Backtrack',
  capabilities: [],
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'Backtrack',
      data: { placement: 'right', width: '360px' },
      render: () => h(Pane, { ctx }),
    })
  },
}
