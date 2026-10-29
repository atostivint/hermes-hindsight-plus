/** Hermes Hindsight Plus — injected-memory pane for Hermes Desktop.
 *
 * The agent half records each successful injection in a bounded local audit.
 * This pane reads that audit through the plugin's dashboard API and refreshes
 * on the plugin's metadata-only gateway event. Message content stays on disk:
 * the live event only says which session changed.
 *
 * Plain ESM, no build step. Only `@hermes/plugin-sdk`, `react` and
 * `react/jsx-runtime` resolve.
 */
import {
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  CopyButton,
  EmptyState,
  ErrorState,
  Loader,
  PALETTE_AREA,
  PANES_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  ScrollArea,
  Streamdown,
  atom,
  host,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const ID = 'hermes-hindsight-plus'
const EVENT = `plugin.${ID}.injection.recorded`
const PANE_ID = `${ID}:memory`

let rest = null
const liveRevision = atom(0)

const styles = {
  root: {
    display: 'flex',
    flexDirection: 'column',
    height: '100%',
    minHeight: 0,
    boxSizing: 'border-box',
    background: 'var(--ui-panel-background, var(--ui-bg-primary))',
    color: 'var(--ui-text-primary)'
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    flexShrink: 0,
    padding: '9px 12px',
    borderBottom: '1px solid var(--ui-stroke-secondary)'
  },
  title: {
    fontSize: 12,
    fontWeight: 600,
    lineHeight: 1.4
  },
  subtitle: {
    marginTop: 1,
    fontSize: 10,
    lineHeight: 1.4,
    color: 'var(--ui-text-tertiary)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis'
  },
  body: {
    flex: 1,
    minHeight: 0,
    display: 'flex',
    flexDirection: 'column'
  },
  centered: {
    flex: 1,
    minHeight: 0,
    display: 'grid',
    placeItems: 'center',
    padding: 16
  },
  list: {
    display: 'grid',
    gap: 8,
    padding: 10
  },
  card: {
    overflow: 'hidden',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: 7,
    background: 'var(--ui-surface-background, var(--ui-bg-secondary))'
  },
  cardHeader: {
    width: '100%',
    height: 'auto',
    justifyContent: 'flex-start',
    padding: '8px 10px',
    borderRadius: 0
  },
  cardHeaderInner: {
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    width: '100%',
    minWidth: 0,
    textAlign: 'left'
  },
  cardHeading: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'flex-start',
    gap: 1,
    flex: 1,
    minWidth: 0
  },
  cardWhen: {
    fontSize: 11,
    fontWeight: 600,
    lineHeight: 1.3,
    color: 'var(--ui-text-primary)'
  },
  cardMeta: {
    width: '100%',
    fontSize: 10,
    lineHeight: 1.35,
    color: 'var(--ui-text-tertiary)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis'
  },
  cardBody: {
    display: 'grid',
    gap: 8,
    padding: '0 10px 10px'
  },
  preview: {
    paddingLeft: 8,
    borderLeft: '2px solid var(--ui-stroke-secondary)',
    fontSize: 11,
    lineHeight: 1.45,
    color: 'var(--ui-text-secondary)',
    whiteSpace: 'pre-wrap',
    overflowWrap: 'anywhere'
  },
  content: {
    maxHeight: 480,
    overflow: 'auto',
    fontSize: 12,
    lineHeight: 1.55,
    color: 'var(--ui-text-primary)',
    overflowWrap: 'anywhere'
  },
  pre: {
    margin: 0,
    padding: 8,
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: 4,
    background: 'var(--ui-bg-primary)',
    fontFamily:
      'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
    fontSize: 11,
    lineHeight: 1.5,
    whiteSpace: 'pre-wrap',
    overflowWrap: 'anywhere'
  },
  note: {
    padding: '6px 8px',
    borderRadius: 4,
    background: 'var(--ui-bg-tertiary)',
    fontSize: 11,
    lineHeight: 1.45,
    color: 'var(--ui-text-secondary)'
  },
  footer: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    flexWrap: 'wrap',
    paddingTop: 1
  },
  footerText: {
    marginLeft: 'auto',
    fontSize: 10,
    color: 'var(--ui-text-tertiary)',
    whiteSpace: 'nowrap'
  }
}

function useScope() {
  const stored = useValue(host.state.focusedStoredSessionId)
  const live = useValue(host.state.focusedSessionId)
  return String(stored || live || '').trim()
}

function useHistory(scope) {
  const revision = useValue(liveRevision)
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: [ID, 'history', scope],
    queryFn: () => {
      if (!rest) {
        return Promise.reject(new Error('The plugin API is not available yet.'))
      }
      return rest(
        `/history?session_id=${encodeURIComponent(scope)}&limit=200`,
        { timeoutMs: 15000 }
      )
    },
    enabled: Boolean(scope),
    staleTime: 5000,
    refetchInterval: 15000,
    retry: 1
  })

  useEffect(() => {
    if (!scope || revision === 0) return
    void queryClient.invalidateQueries({ queryKey: [ID, 'history', scope] })
  }, [queryClient, revision, scope])

  return query
}

function formatNumber(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toLocaleString() : '0'
}

function formatWhen(value) {
  if (!value) return 'Unknown time'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  const elapsed = Date.now() - date.getTime()
  if (elapsed >= 0 && elapsed < 60_000) return 'just now'
  if (elapsed >= 0 && elapsed < 3_600_000) {
    return `${Math.max(1, Math.floor(elapsed / 60_000))}m ago`
  }
  if (elapsed >= 0 && elapsed < 86_400_000) {
    return `${Math.floor(elapsed / 3_600_000)}h ago`
  }
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  })
}

function fullWhen(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value || '') : date.toLocaleString()
}

function modelText(models) {
  const names = Array.isArray(models)
    ? models
        .map(model => (model && (model.name || model.id)) || '')
        .filter(Boolean)
    : []
  if (names.length === 1) return names[0]
  if (names.length > 1) return `${names.length} models`
  return 'model metadata unavailable'
}

function entryMeta(entry) {
  const bits = [entry.is_first_turn ? 'First turn' : 'Later turn']
  bits.push(modelText(entry.models))
  bits.push(`${formatNumber(entry.injected_chars)} chars`)
  if (entry.context_note_chars > 0) bits.push('deployment note')
  return bits.join(' • ')
}

function entryDetails(entry) {
  const bits = [
    `Injected ${formatNumber(entry.injected_chars)} of ${formatNumber(entry.assembled_chars)} characters`,
    `budget ${formatNumber(entry.max_chars)}`
  ]
  if (entry.truncated) bits.push('truncated to fit')
  if (entry.turn_id) bits.push(`turn ${entry.turn_id}`)
  if (entry.model) bits.push(`model ${entry.model}`)
  if (entry.platform) bits.push(`platform ${entry.platform}`)
  return bits.join(' • ')
}

function InjectionEntry({ entry, initiallyOpen }) {
  const [open, setOpen] = useState(Boolean(initiallyOpen))
  const [raw, setRaw] = useState(false)
  const content =
    typeof entry.content === 'string' && entry.content.length > 0
      ? entry.content
      : ''

  return jsxs('section', {
    style: styles.card,
    children: [
      jsx(Button, {
        type: 'button',
        variant: 'ghost',
        size: 'sm',
        onClick: () => setOpen(value => !value),
        'aria-expanded': open,
        style: styles.cardHeader,
        children: jsxs('span', {
          style: styles.cardHeaderInner,
          children: [
            jsx(Codicon, {
              name: open ? 'chevron-down' : 'chevron-right',
              size: '0.85rem'
            }),
            jsxs('span', {
              style: styles.cardHeading,
              children: [
                jsx('span', {
                  style: styles.cardWhen,
                  title: fullWhen(entry.recorded_at),
                  children: formatWhen(entry.recorded_at)
                }),
                jsx('span', {
                  style: styles.cardMeta,
                  children: entryMeta(entry)
                })
              ]
            }),
            entry.truncated
              ? jsx(Badge, {
                  variant: 'warn',
                  size: 'xs',
                  children: 'truncated'
                })
              : null,
            entry.content_recorded
              ? null
              : jsx(Badge, {
                  variant: 'muted',
                  size: 'xs',
                  children: 'metadata'
                })
          ]
        })
      }),
      open
        ? jsxs('div', {
            style: styles.cardBody,
            children: [
              entry.user_preview
                ? jsx('div', {
                    style: styles.preview,
                    children: `“${entry.user_preview}”`
                  })
                : null,
              entry.content_recorded
                ? content
                  ? jsx('div', {
                      style: styles.content,
                      children: raw
                        ? jsx('pre', {
                            style: styles.pre,
                            children: content
                          })
                        : jsx(Streamdown, {
                            controls: false,
                            mode: 'static',
                            parseIncompleteMarkdown: false,
                            children: content
                          })
                    })
                  : jsx('div', {
                      style: styles.note,
                      children:
                        'This audit entry has content recording enabled but no content payload.'
                    })
                : jsx('div', {
                    style: styles.note,
                    children:
                      'Content recording is off for this entry. Turn, model, and size metadata are still available.'
                  }),
              jsxs('div', {
                style: styles.footer,
                children: [
                  content
                    ? jsx(CopyButton, {
                        text: content,
                        appearance: 'button',
                        buttonSize: 'xs',
                        buttonVariant: 'secondary',
                        children: 'Copy'
                      })
                    : null,
                  content
                    ? jsxs(Button, {
                        type: 'button',
                        variant: 'ghost',
                        size: 'xs',
                        onClick: () => setRaw(value => !value),
                        title: raw
                          ? 'Show rendered Markdown'
                          : 'Show the raw injected text',
                        children: [
                          jsx(Codicon, {
                            name: raw ? 'markdown' : 'code',
                            size: '0.75rem'
                          }),
                          raw ? 'Rendered' : 'Raw'
                        ]
                      })
                    : null,
                  jsx('span', {
                    style: styles.footerText,
                    title: entryDetails(entry),
                    children: `${formatNumber(entry.injected_chars)} injected`
                  })
                ]
              })
            ]
          })
        : null
    ]
  })
}

function MemoryTabTitle() {
  const scope = useScope()
  const history = useHistory(scope)
  const count =
    history.data?.session?.entry_count ??
    (Array.isArray(history.data?.entries) ? history.data.entries.length : 0)

  return jsxs('span', {
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 5,
      minWidth: 0
    },
    children: [
      jsx(Codicon, { name: 'book', size: '0.85rem' }),
      jsx('span', { children: 'Memory' }),
      count > 0
        ? jsx(Badge, { size: 'xs', variant: 'muted', children: String(count) })
        : null
    ]
  })
}

function PaneBody({ scope, history }) {
  const { data, error, isFetching, isLoading, refetch } = history
  const entries = Array.isArray(data?.entries) ? data.entries : []

  if (!scope) {
    return jsx('div', {
      style: styles.centered,
      children: jsx(EmptyState, {
        title: 'No chat selected',
        description:
          'Open a chat to see the Hindsight memory injected into it.'
      })
    })
  }

  if (isLoading && !data) {
    return jsx('div', {
      style: styles.centered,
      children: jsx(Loader, { label: 'Loading injected memory', style: { width: 24, height: 24 } })
    })
  }

  if (error && !data) {
    return jsx('div', {
      style: styles.centered,
      children: jsx(ErrorState, {
        title: 'Could not load memory history',
        description: String(error.message || error),
        children: jsx(Button, {
          type: 'button',
          variant: 'secondary',
          size: 'sm',
          disabled: isFetching,
          onClick: () => void refetch(),
          children: 'Try again'
        })
      })
    })
  }

  if (entries.length === 0) {
    return jsx('div', {
      style: styles.centered,
      children: jsx(EmptyState, {
        title: 'No injections recorded',
        description:
          'Memory appears here after the plugin adds Hindsight context to a turn. With the default first_turn mode that happens once per new chat.'
      })
    })
  }

  return jsx(ScrollArea, {
    style: { flex: 1, minHeight: 0 },
    children: jsx('div', {
      style: styles.list,
      children: entries.map((entry, index) =>
        jsx(
          InjectionEntry,
          { entry, initiallyOpen: index === 0 },
          entry.id || `${entry.recorded_at || 'entry'}-${index}`
        )
      )
    })
  })
}

function MemoryPane() {
  const scope = useScope()
  const history = useHistory(scope)
  const data = history.data
  const entries = Array.isArray(data?.entries) ? data.entries : []
  const latest = entries[0]
  const subtitle = latest
    ? `${entries.length} ${entries.length === 1 ? 'injection' : 'injections'} • latest ${formatWhen(latest.recorded_at)}`
    : 'Injected Hindsight context for this chat'

  return jsxs('div', {
    style: styles.root,
    children: [
      jsxs('header', {
        style: styles.header,
        children: [
          jsxs('div', {
            style: { flex: 1, minWidth: 0 },
            children: [
              jsx('div', { style: styles.title, children: 'Injected memory' }),
              jsx('div', {
                style: styles.subtitle,
                title: subtitle,
                children: subtitle
              })
            ]
          }),
          jsx(Button, {
            type: 'button',
            variant: 'ghost',
            size: 'icon-xs',
            title: 'Refresh memory history',
            'aria-label': 'Refresh memory history',
            disabled: history.isFetching,
            onClick: () => void history.refetch(),
            children: jsx(Codicon, {
              name: 'refresh',
              size: '0.8rem',
              spinning: history.isFetching
            })
          })
        ]
      }),
      jsx('div', {
        style: styles.body,
        children: jsx(PaneBody, { scope, history })
      })
    ]
  })
}

// ==== Memory review (opt-in page) ============================================
// Lists a Hindsight bank's facts with staleness scores and lets the user revoke
// (soft), restore, or rewrite them. Backend: dashboard/plugin_api.py `/review/*`;
// the Hindsight key stays server-side. Every action asks for confirmation and
// can be undone from the `previous` state the backend returns. "Check with
// Hermes" opens a normal chat and only ever shows verdicts as badges: nothing is
// revoked automatically. Kept in one delimited block so a rebase stays easy.

const REVIEW_PATH = '/memory-review'
const REVIEW_BANKS = ['hermes', 'atlas', 'astro']
const REVIEW_MAX_CHECK = 10
const VERDICT_VARIANT = {
  'still true': 'default',
  outdated: 'warn',
  'cannot tell': 'muted'
}

const rv = {
  page: { ...styles.root, padding: 0 },
  bar: {
    display: 'flex',
    alignItems: 'center',
    flexWrap: 'wrap',
    gap: 8,
    padding: '9px 12px',
    borderBottom: '1px solid var(--ui-stroke-secondary)'
  },
  field: {
    padding: '4px 6px',
    borderRadius: 4,
    border: '1px solid var(--ui-stroke-secondary)',
    background: 'var(--ui-bg-primary)',
    color: 'var(--ui-text-primary)',
    fontSize: 12
  },
  row: {
    display: 'grid',
    gridTemplateColumns: 'auto 1fr',
    gap: 8,
    padding: '8px 10px',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: 7,
    background: 'var(--ui-surface-background, var(--ui-bg-secondary))'
  },
  text: { fontSize: 12, lineHeight: 1.5, overflowWrap: 'anywhere' },
  actions: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' },
  textarea: {
    width: '100%',
    minHeight: 90,
    boxSizing: 'border-box',
    padding: 8,
    borderRadius: 4,
    border: '1px solid var(--ui-stroke-secondary)',
    background: 'var(--ui-bg-primary)',
    color: 'var(--ui-text-primary)',
    fontSize: 12
  }
}

function reviewCall(path, options = {}) {
  if (!rest) return Promise.reject(new Error('The plugin API is not available yet.'))
  return rest(path, { timeoutMs: 30000, ...options })
}

function reviewPost(path, body) {
  return reviewCall(path, { method: 'POST', body })
}

function pct(value) {
  return `${Math.round((Number(value) || 0) * 100)}%`
}

function sortReview(items, key) {
  const field = { age: 'age_score', volatile: 'volatile_score', staleness: 'staleness' }[key]
  return [...items].sort((a, b) => (b[field] || 0) - (a[field] || 0))
}

// Collect every string in a gateway snapshot; used to find the agent's json block.
function collectStrings(value, out = []) {
  if (typeof value === 'string') out.push(value)
  else if (Array.isArray(value)) value.forEach(v => collectStrings(v, out))
  else if (value && typeof value === 'object') Object.values(value).forEach(v => collectStrings(v, out))
  return out
}

function ReviewRow({ item, checked, onCheck, onAction, verdict }) {
  const volatile = item.volatile_hits.length ? item.volatile_hits.join(', ') : 'none'
  const invalid = item.state === 'invalidated'
  return jsxs('section', {
    style: { ...rv.row, opacity: invalid ? 0.7 : 1 },
    children: [
      jsx('input', {
        type: 'checkbox',
        checked,
        disabled: !item.curatable,
        'aria-label': 'Select for check',
        onChange: e => onCheck(item.id, e.target.checked)
      }),
      jsxs('div', {
        style: { display: 'grid', gap: 6, minWidth: 0 },
        children: [
          jsx('div', { style: rv.text, children: item.text }),
          jsxs('div', {
            style: rv.actions,
            children: [
              jsx(Badge, {
                size: 'xs',
                variant: 'muted',
                title: item.date,
                children: item.age_days == null ? 'age unknown' : `${item.age_days} d (${pct(item.age_score)})`
              }),
              jsx(Badge, {
                size: 'xs',
                variant: item.volatile_score > 0 ? 'warn' : 'muted',
                title: `volatile: ${volatile}`,
                children: `volatile ${pct(item.volatile_score)}`
              }),
              invalid
                ? jsx(Badge, { size: 'xs', variant: 'warn', title: item.invalidation_reason || '', children: 'revoked' })
                : null,
              verdict
                ? jsx(Badge, {
                    size: 'xs',
                    variant: VERDICT_VARIANT[verdict.verdict] || 'muted',
                    title: verdict.evidence || '',
                    children: verdict.verdict
                  })
                : null,
              item.curatable
                ? jsxs('span', {
                    style: { ...rv.actions, marginLeft: 'auto' },
                    children: [
                      invalid
                        ? jsx(Button, { type: 'button', size: 'xs', variant: 'secondary', onClick: () => onAction('restore', item), children: 'Restore' })
                        : jsx(Button, { type: 'button', size: 'xs', variant: 'secondary', onClick: () => onAction('revoke', item), children: 'Revoke' }),
                      jsx(Button, { type: 'button', size: 'xs', variant: 'ghost', onClick: () => onAction('update', item), children: 'Edit' })
                    ]
                  })
                : jsx(Badge, { size: 'xs', variant: 'muted', children: 'derived' })
            ]
          })
        ]
      })
    ]
  })
}

// One confirm dialog for all three actions; builds the request and the undo.
function ActionDialog({ action, onClose, onDone }) {
  const [reasonKind, setReasonKind] = useState('outdated: ')
  const [reasonText, setReasonText] = useState('')
  const [text, setText] = useState(action.item.text)
  const { kind, item, bank } = action
  const base = `/review/${bank}/memories/${encodeURIComponent(item.id)}`

  const title = { revoke: 'Revoke this fact?', restore: 'Restore this fact?', update: 'Rewrite this fact?' }[kind]
  const description = {
    revoke: 'Soft revoke: the fact stays in Hindsight (kept for audit) but is excluded from recall. You can restore it.',
    restore: 'The fact becomes valid again and is recalled normally.',
    update: 'The new text is re-embedded and its derived observations are dropped and rebuilt. Undo rewrites the old text back.'
  }[kind]

  const run = async () => {
    let result
    if (kind === 'revoke') {
      result = await reviewPost(`${base}/revoke`, { reason: reasonKind + reasonText.trim() })
    } else if (kind === 'restore') {
      result = await reviewPost(`${base}/restore`, {})
    } else {
      result = await reviewPost(`${base}/update`, { text })
    }
    onDone({ kind, bank, previous: result.previous, current: result.current })
  }

  return jsxs(ConfirmDialog, {
    open: true,
    onClose,
    onConfirm: run,
    title,
    description,
    confirmLabel: { revoke: 'Revoke', restore: 'Restore', update: 'Save' }[kind],
    destructive: kind === 'revoke',
    children: jsxs('div', {
      style: { display: 'grid', gap: 8 },
      children: [
        jsx('div', { style: styles.preview, children: item.text }),
        kind === 'revoke'
          ? jsxs('div', {
              style: rv.actions,
              children: [
                jsxs('select', {
                  style: rv.field,
                  value: reasonKind,
                  onChange: e => setReasonKind(e.target.value),
                  children: [
                    jsx('option', { value: 'outdated: ', children: 'outdated:' }),
                    jsx('option', { value: 'superseded by ', children: 'superseded by' })
                  ]
                }),
                jsx('input', {
                  style: { ...rv.field, flex: 1, minWidth: 160 },
                  value: reasonText,
                  placeholder: 'reason (required)',
                  onChange: e => setReasonText(e.target.value)
                })
              ]
            })
          : null,
        kind === 'update'
          ? jsx('textarea', { style: rv.textarea, value: text, onChange: e => setText(e.target.value) })
          : null
      ]
    })
  })
}

async function startHermesCheck(bank, facts) {
  const { prompt } = await reviewPost('/review/check-prompt', {
    facts: facts.map(f => ({ id: f.id, text: f.text }))
  })
  try {
    const snapshot = await host.request('session.create', { title: `Memory check (${bank})` })
    const sessionId = snapshot && snapshot.session_id
    if (!sessionId) throw new Error('session.create returned no session id')
    await host.request('prompt.submit', { session_id: sessionId, text: prompt })
    if (typeof host.openSession === 'function') {
      await host.openSession(snapshot.stored_session_id || sessionId)
    }
    return { sessionId, mode: 'session' }
  } catch (error) {
    // Fallback: a fresh chat with the prompt in the composer; the user presses Enter.
    host.newChat()
    await host.composer.setDraft('new', prompt)
    return { sessionId: null, mode: 'draft', error: String(error && error.message ? error.message : error) }
  }
}

function MemoryReviewPage() {
  const [bank, setBank] = useState('hermes')
  const [state, setState] = useState('valid')
  const [sort, setSort] = useState('staleness')
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState({})
  const [action, setAction] = useState(null)
  const [last, setLast] = useState(null)
  const [verdicts, setVerdicts] = useState({})
  const [check, setCheck] = useState(null)
  const [pasted, setPasted] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const queryClient = useQueryClient()
  const key = [ID, 'review', bank, state, offset]

  const query = useQuery({
    queryKey: key,
    queryFn: () =>
      reviewCall(`/review/${bank}/memories?limit=100&offset=${offset}&state=${state}&sort=staleness`),
    staleTime: 10000,
    retry: 1
  })

  const items = sortReview(Array.isArray(query.data?.items) ? query.data.items : [], sort)
  const picked = items.filter(i => selected[i.id])
  const refresh = () => void queryClient.invalidateQueries({ queryKey: [ID, 'review'] })

  const onCheck = (id, on) => {
    const count = Object.values(selected).filter(Boolean).length
    if (on && count >= REVIEW_MAX_CHECK) {
      setNotice(`Select at most ${REVIEW_MAX_CHECK} facts to check.`)
      return
    }
    setNotice('')
    setSelected(prev => ({ ...prev, [id]: on }))
  }

  const undo = async () => {
    if (!last) return
    const { kind, bank: b, previous } = last
    const base = `/review/${b}/memories/${encodeURIComponent(previous.id)}`
    setBusy(true)
    try {
      if (kind === 'revoke') await reviewPost(`${base}/restore`, {})
      else if (kind === 'restore') await reviewPost(`${base}/revoke`, { reason: previous.invalidation_reason || 'outdated: restored change reverted' })
      else await reviewPost(`${base}/update`, { text: previous.text })
      setLast(null)
      setNotice('Change undone.')
      refresh()
    } catch (error) {
      setNotice(`Undo failed: ${String(error.message || error)}`)
    } finally {
      setBusy(false)
    }
  }

  const runCheck = async () => {
    setBusy(true)
    setNotice('')
    try {
      const started = await startHermesCheck(bank, picked)
      setCheck({ ids: picked.map(f => f.id), ...started })
      setNotice(
        started.mode === 'session'
          ? 'Check started in a Hermes chat. When it answers, use "Read verdicts".'
          : 'Could not start the chat directly; the prompt is in a new chat composer. Press Enter there.'
      )
    } catch (error) {
      setNotice(`Could not start the check: ${String(error.message || error)}`)
    } finally {
      setBusy(false)
    }
  }

  const readVerdicts = async () => {
    if (!check) return
    setBusy(true)
    try {
      let reply = pasted
      if (!reply && check.sessionId) {
        const snapshot = await host.request('session.resume', { session_id: check.sessionId })
        reply = collectStrings(snapshot).filter(s => s.includes('```json')).pop() || ''
      }
      const parsed = await reviewPost('/review/verdicts', { reply, ids: check.ids })
      setVerdicts(prev => ({ ...prev, ...parsed.verdicts }))
      setNotice(
        Object.keys(parsed.verdicts).length
          ? 'Verdicts loaded. They are advice only: nothing was changed.'
          : 'No verdict block found yet. Paste the agent reply below, or try again later.'
      )
    } catch (error) {
      setNotice(`Could not read verdicts: ${String(error.message || error)}`)
    } finally {
      setBusy(false)
    }
  }

  const select = (value, onChange, options) =>
    jsx('select', {
      style: rv.field,
      value,
      onChange: e => onChange(e.target.value),
      children: options.map(([v, label]) => jsx('option', { value: v, children: label }, v))
    })

  return jsxs('div', {
    style: rv.page,
    children: [
      jsxs('header', {
        style: rv.bar,
        children: [
          jsx('div', { style: { ...styles.title, flex: 1 }, children: 'Memory review' }),
          select(bank, v => { setBank(v); setOffset(0); setSelected({}) }, REVIEW_BANKS.map(b => [b, b])),
          select(state, v => { setState(v); setOffset(0) }, [['valid', 'valid'], ['invalidated', 'revoked']]),
          select(sort, setSort, [['staleness', 'Sort: staleness'], ['age', 'Sort: age'], ['volatile', 'Sort: volatile']]),
          jsx(Button, {
            type: 'button', size: 'xs', variant: 'secondary',
            disabled: busy || picked.length === 0 || picked.length > REVIEW_MAX_CHECK,
            onClick: () => void runCheck(),
            children: `Check with Hermes (${picked.length})`
          }),
          check
            ? jsx(Button, { type: 'button', size: 'xs', variant: 'ghost', disabled: busy, onClick: () => void readVerdicts(), children: 'Read verdicts' })
            : null,
          jsx(Button, {
            type: 'button', size: 'icon-xs', variant: 'ghost', title: 'Refresh', 'aria-label': 'Refresh',
            disabled: query.isFetching, onClick: refresh,
            children: jsx(Codicon, { name: 'refresh', size: '0.8rem', spinning: query.isFetching })
          })
        ]
      }),
      notice || last
        ? jsxs('div', {
            style: { ...styles.note, margin: '8px 12px 0', display: 'flex', alignItems: 'center', gap: 8 },
            children: [
              jsx('span', { style: { flex: 1 }, children: notice || `Last change: ${last.kind} (${last.previous.id.slice(0, 8)})` }),
              last ? jsx(Button, { type: 'button', size: 'xs', variant: 'secondary', disabled: busy, onClick: () => void undo(), children: 'Undo' }) : null
            ]
          })
        : null,
      check
        ? jsx('textarea', {
            style: { ...rv.textarea, margin: '8px 12px 0', width: 'calc(100% - 24px)', minHeight: 36 },
            placeholder: 'Fallback: paste the agent reply (with its json block) here, then "Read verdicts"',
            value: pasted,
            onChange: e => setPasted(e.target.value)
          })
        : null,
      jsx('div', {
        style: styles.body,
        children: query.isLoading
          ? jsx('div', { style: styles.centered, children: jsx(Loader, { label: 'Loading memories', style: { width: 24, height: 24 } }) })
          : query.error
            ? jsx('div', {
                style: styles.centered,
                children: jsx(ErrorState, {
                  title: 'Could not load memories',
                  description: String(query.error.message || query.error),
                  children: jsx(Button, { type: 'button', variant: 'secondary', size: 'sm', onClick: () => void query.refetch(), children: 'Try again' })
                })
              })
            : items.length === 0
              ? jsx('div', { style: styles.centered, children: jsx(EmptyState, { title: 'No facts here', description: 'Nothing to review in this bank and state.' }) })
              : jsx(ScrollArea, {
                  style: { flex: 1, minHeight: 0 },
                  children: jsxs('div', {
                    style: styles.list,
                    children: [
                      ...items.map(item =>
                        jsx(ReviewRow, {
                          key: item.id, item, checked: Boolean(selected[item.id]), verdict: verdicts[item.id],
                          onCheck, onAction: (kind, it) => setAction({ kind, item: it, bank })
                        })
                      ),
                      jsxs('div', {
                        style: rv.actions,
                        children: [
                          jsx(Button, { type: 'button', size: 'xs', variant: 'ghost', disabled: offset === 0, onClick: () => setOffset(Math.max(0, offset - 100)), children: 'Newer' }),
                          jsx('span', { style: styles.footerText, children: `${offset + 1}-${offset + items.length} of ${formatNumber(query.data?.total)}` }),
                          jsx(Button, { type: 'button', size: 'xs', variant: 'ghost', disabled: items.length < 100, onClick: () => setOffset(offset + 100), children: 'Older' })
                        ]
                      })
                    ]
                  })
                })
      }),
      action
        ? jsx(ActionDialog, {
            key: `${action.kind}-${action.item.id}`,
            action,
            onClose: () => setAction(null),
            onDone: done => { setAction(null); setLast(done); setNotice(''); refresh() }
          })
        : null
    ]
  })
}

// ==== end Memory review ======================================================

export default {
  id: ID,
  name: 'Hindsight Memory',
  description:
    'Shows the Hindsight mental-model context injected into the focused chat, and offers an opt-in Memory review page.',
  // Opt-in: off until enabled in Settings > Plugins (this also gates the memory pane).
  defaultEnabled: false,
  register(ctx) {
    rest = ctx.rest

    ctx.onEvent(EVENT, () => {
      liveRevision.set(liveRevision.get() + 1)
    })

    ctx.onDispose(() => {
      rest = null
      liveRevision.set(0)
    })

    ctx.register({
      id: 'memory',
      area: PANES_AREA,
      order: 140,
      title: 'Hindsight memory',
      data: {
        placement: 'main',
        dock: { pane: 'workspace', pos: 'right' },
        width: '360px',
        minWidth: '280px',
        defaultCollapsed: true,
        tabTitle: () => jsx(MemoryTabTitle, {}),
        tabTitleText: () => 'Memory'
      },
      render: () => jsx(MemoryPane, {})
    })

    ctx.register({
      id: 'review-page',
      area: ROUTES_AREA,
      title: 'Memory review',
      data: { path: REVIEW_PATH },
      render: () => jsx(MemoryReviewPage, {})
    })

    ctx.register({
      id: 'review-nav',
      area: SIDEBAR_NAV_AREA,
      order: 60,
      data: { codicon: 'checklist', label: 'Memory review', path: REVIEW_PATH }
    })

    ctx.register({
      id: 'open-review',
      area: PALETTE_AREA,
      data: {
        id: `${ID}.open-review`,
        label: 'Open Hindsight memory review',
        keywords: ['hindsight', 'memory', 'review', 'stale', 'revoke'],
        run: () => host.navigate(REVIEW_PATH)
      }
    })

    ctx.register({
      id: 'open-memory',
      area: PALETTE_AREA,
      data: {
        id: `${ID}.open-memory`,
        label: 'Show injected Hindsight memory',
        keywords: ['hindsight', 'memory', 'mental model', 'injection', 'context'],
        run: () => {
          if (typeof host.revealPane === 'function') host.revealPane(PANE_ID)
        }
      }
    })
  }
}
