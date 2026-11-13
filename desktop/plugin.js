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
  Input,
  Loader,
  PALETTE_AREA,
  PANES_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  ScrollArea,
  Streamdown,
  Textarea,
  atom,
  host,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useRef, useState } from 'react'

const ID = 'hermes-hindsight-plus'
const EVENT = `plugin.${ID}.injection.recorded`
const PANE_ID = `${ID}:memory`

let rest = null
const liveRevision = atom(0)
const providerRecalls = atom([])

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
  // One box per memory: a `## <name>` heading and all of its paragraphs share
  // one outline, instead of one long paragraph flow.
  block: {
    padding: '6px 8px',
    marginBottom: 6,
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: 6,
    background: 'var(--ui-surface-background, transparent)'
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

function useProviderEntries(scope) {
  const recalls = useValue(providerRecalls)
  if (!scope || !Array.isArray(recalls)) return []
  return recalls.filter(entry => {
    if (!entry) return false
    const sid = String(entry.session_id || '')
    return sid === scope || sid.endsWith(scope) || scope.endsWith(sid)
  })
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

function useInjections(scope) {
  const revision = useValue(liveRevision)
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: [ID, 'injections', scope],
    queryFn: () => {
      if (!rest) {
        return Promise.reject(new Error('The plugin API is not available yet.'))
      }
      return rest(
        `/injections?session=${encodeURIComponent(scope)}&limit=200`,
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
    void queryClient.invalidateQueries({ queryKey: [ID, 'injections', scope] })
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
  const bits = [entry.source || 'injection', entry.status || 'injected']
  if (entry.bank) bits.push(entry.bank)
  if (entry.count != null) bits.push(`${formatNumber(entry.count)} memories`)
  else bits.push('—')
  if (entry.injected_chars != null) bits.push(`${formatNumber(entry.injected_chars)} chars`)
  return bits.join(' • ')
}

function entryDetails(entry) {
  const bits = []
  if (entry.bank) bits.push(`bank ${entry.bank}`)
  if (entry.count != null) bits.push(`${formatNumber(entry.count)} memories`)
  else bits.push('—')
  if (entry.injected_chars != null) {
    bits.push(`injected ${formatNumber(entry.injected_chars)} chars`)
  } else {
    bits.push('size unavailable')
  }
  if (entry.truncated) bits.push('truncated to fit')
  if (entry.turn_id) bits.push(`turn ${entry.turn_id}`)
  if (entry.model) bits.push(`model ${entry.model}`)
  if (entry.platform) bits.push(`platform ${entry.platform}`)
  if (entry.content_recorded === false && entry.source === 'plugin mental-models') {
    bits.push('content recording off')
  }
  return bits.join(' • ')
}

function normalizePluginEntry(entry, bank) {
  const models = Array.isArray(entry && entry.models) ? entry.models : []
  return {
    id: entry && entry.id,
    recorded_at: entry && entry.recorded_at,
    source: 'plugin mental-models',
    bank: (entry && entry.bank) || bank || '—',
    count: models.length,
    injected_chars:
      entry && entry.injected_chars != null ? entry.injected_chars : null,
    status:
      entry && entry.truncated
        ? 'truncated'
        : entry && Number(entry.injected_chars) > 0
          ? 'injected'
          : 'empty',
    truncated: Boolean(entry && entry.truncated),
    content: entry && typeof entry.content === 'string' ? entry.content : '',
    content_recorded: Boolean(entry && entry.content_recorded),
    user_preview: (entry && entry.user_preview) || '',
    turn_id: entry && entry.turn_id,
    model: entry && entry.model,
    platform: entry && entry.platform,
    models
  }
}

function normalizeProviderEntry(entry, bank) {
  const text = entry && entry.text ? String(entry.text) : ''
  const match = text.match(/recalled\\s+(\\d+)\\s+memor/i)
  const count = match
    ? Number(match[1])
    : /recalled relevant memory/i.test(text)
      ? 1
      : 0
  return {
    id: entry && entry.id,
    recorded_at: (entry && entry.recorded_at) || new Date().toISOString(),
    source: 'provider recall',
    bank: (entry && entry.bank) || bank || '—',
    count,
    injected_chars:
      entry && entry.injected_chars != null ? entry.injected_chars : null,
    status: (entry && entry.status) || 'injected',
    truncated: false,
    content: (entry && entry.content) || '',
    content_recorded: false,
    user_preview: '',
    turn_id: (entry && entry.turn_id) || '',
    model: '',
    platform: (entry && entry.platform) || '',
    models: []
  }
}

function normalizeStateEntry(entry, bank) {
  const source =
    entry && entry.source === 'provider recall'
      ? 'provider recall'
      : 'plugin mental-models'
  const text = entry && entry.text ? String(entry.text) : ''
  const count =
    entry && entry.count != null
      ? entry.count
      : source === 'plugin mental-models'
        ? (text.match(/^## /gm) || []).length
        : null
  return {
    id: entry && entry.id,
    recorded_at: (entry && entry.recorded_at) || new Date().toISOString(),
    source,
    bank: (entry && entry.bank) || bank || '—',
    count,
    injected_chars:
      entry && entry.injected_chars != null ? entry.injected_chars : text.length,
    status: (entry && entry.status) || 'unknown',
    truncated: (entry && entry.status) === 'truncated',
    content: text,
    text,
    turn_id: (entry && entry.turn_id) || '',
    model: '',
    platform: '',
    models: []
  }
}

function dedupeInjectionEntries(entries) {
  const seen = new Set()
  const out = []
  for (const entry of entries) {
    if (!entry) continue
    const date = new Date(entry.recorded_at)
    const minute = Number.isNaN(date.getTime())
      ? String(entry.recorded_at || '')
      : date.toISOString().slice(0, 16)
    const key = `${entry.source || ''}|${minute}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(entry)
  }
  return out
}

function useHealthBank() {
  const query = useQuery({
    queryKey: [ID, 'health'],
    queryFn: () => {
      if (!rest) return Promise.reject(new Error('The plugin API is not available yet.'))
      return rest('/health', { timeoutMs: 5000 })
    },
    enabled: Boolean(rest),
    staleTime: 30000,
    retry: 1
  })
  return query && query.data ? query.data.bank : null
}

// The configured recall preamble repeats on every entry and carries no memory
// of its own, so the pane fetches it once and drops the matching block. A
// missing route or an offline API resolves to '' and drops nothing.
function useRecallPreamble() {
  const query = useQuery({
    queryKey: [ID, 'recall-preamble'],
    queryFn: () =>
      reviewCall('/recall-preamble', { timeoutMs: 5000 })
        .then(data =>
          data && typeof data.preamble === 'string' ? data.preamble : ''
        )
        .catch(() => ''),
    enabled: Boolean(rest),
    staleTime: Infinity,
    retry: false
  })
  return typeof query.data === 'string' ? query.data : ''
}

// The injected text is a preamble, then one `## <name>` section per memory
// (`core.py`), each heading followed by its paragraphs. Compare on the first
// 200 characters so a truncated (…) preamble still matches; case-sensitive,
// trimmed.
function isPreambleBlock(block, preamble) {
  const left = String(block || '').trim()
  const right = String(preamble || '').trim()
  if (!left || !right) return false
  if (left === right) return true
  // A truncated injection cuts the preamble mid-text (trailing …): the first
  // 200 characters are then identical on both sides.
  return left.slice(0, 200) === right.slice(0, 200)
}

// A heading block starts a memory; every following block that is not a heading
// belongs to it, joined back with a blank line so `demoteHeadings` still sees
// one document per card. Text before the first heading is its own group.
function groupInjectionBlocks(blocks) {
  const groups = []
  let current = ''
  for (const block of blocks) {
    if (/^#{1,6}\s/.test(block)) {
      if (current) groups.push(current)
      current = block
    } else {
      current = current ? `${current}\n\n${block}` : block
    }
  }
  if (current) groups.push(current)
  return groups
}

function splitInjectionBlocks(content, preamble) {
  const blocks = String(content || '')
    .split(/\n\s*\n/)
    .map(block => block.trim())
    .filter(Boolean)
  if (blocks.length > 0 && isPreambleBlock(blocks[0], preamble)) blocks.shift()
  return groupInjectionBlocks(blocks)
}

// The pane is ~360px wide: a Markdown heading would render at heading size and
// swallow the whole card. Demote `#`-lines to bold paragraphs for display only
// (Copy still copies the untouched content). Fenced code is left alone, so a
// `# comment` there keeps its own styling.
function demoteHeadings(text) {
  let fenced = false
  return String(text)
    .split('\n')
    .map(line => {
      if (/^\s*(```|~~~)/.test(line)) {
        fenced = !fenced
        return line
      }
      if (fenced) return line
      const match = /^#{1,6}\s+(.*)$/.exec(line)
      if (!match) return line
      const rest = match[1].trim()
      return rest ? `**${rest}**` : ''
    })
    .join('\n')
}

function InjectionEntry({ entry, initiallyOpen, preamble }) {
  const [open, setOpen] = useState(Boolean(initiallyOpen))
  const [raw, setRaw] = useState(false)
  const content =
    typeof entry.content === 'string' && entry.content.length > 0
      ? entry.content
      : ''
  const blocks = content ? splitInjectionBlocks(content, preamble) : []

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
              entry.source === 'provider recall' && !content
                ? jsx('div', { style: styles.note, children: 'Provider recall captured from the live status line. Count and bank are shown above; the injected text is not available in this pane.' })
                : content
                ? jsx('div', {
                    style: styles.content,
                    children: raw
                      ? jsx('pre', {
                          style: styles.pre,
                          children: content
                        })
                      : blocks.length > 0
                        ? jsxs('div', {
                            children: blocks.map((block, index) =>
                              jsx(
                                'div',
                                {
                                  style: styles.block,
                                  children: jsx(Streamdown, {
                                    controls: false,
                                    mode: 'static',
                                    parseIncompleteMarkdown: false,
                                    children: demoteHeadings(block)
                                  })
                                },
                                String(index)
                              )
                            )
                          })
                        : jsx('div', {
                            style: styles.note,
                            children: 'Aucun souvenir à afficher pour cette injection.'
                          })
                  })
                : entry.content_recorded
                  ? jsx('div', {
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
  const injections = useInjections(scope)
  const healthBank = useHealthBank()
  const preamble = useRecallPreamble()
  const bank = (data && data.bank) || (injections.data && injections.data.bank) || healthBank
  const pluginEntries = Array.isArray(data?.entries) ? data.entries : []
  const stateEntries = Array.isArray(injections.data?.entries)
    ? injections.data.entries
    : []
  const stateUnavailable =
    injections.data && injections.data.source === 'unavailable'
  const providerEntries = useProviderEntries(scope).map(entry =>
    normalizeProviderEntry(entry, bank)
  )
  const entries = dedupeInjectionEntries([
    ...pluginEntries.map(entry => normalizePluginEntry(entry, bank)),
    ...stateEntries.map(entry => normalizeStateEntry(entry, bank)),
    ...providerEntries
  ]).sort((a, b) => {
    const left = new Date(b.recorded_at).getTime()
    const right = new Date(a.recorded_at).getTime()
    return (Number.isFinite(left) ? left : 0) - (Number.isFinite(right) ? right : 0)
  })

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

  const unavailableNote = stateUnavailable
    ? jsx('div', {
        style: styles.note,
        children: `Historical entries unavailable: ${injections.data.reason || 'state.db unreadable'}`
      })
    : null

  if (entries.length === 0) {
    return jsxs('div', {
      style: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' },
      children: [
        unavailableNote,
        jsx('div', {
          style: styles.centered,
          children: jsx(EmptyState, {
            title: 'No injections recorded',
            description:
              'Memory appears here after the plugin adds Hindsight context to a turn. With the default first_turn mode that happens once per new chat.'
          })
        })
      ]
    })
  }

  return jsx(ScrollArea, {
    style: { flex: 1, minHeight: 0 },
    children: jsxs('div', {
      style: styles.list,
      children: [
        unavailableNote,
        ...entries.map((entry, index) =>
          jsx(
            InjectionEntry,
            { entry, initiallyOpen: index === 0, preamble },
            entry.id || `${entry.recorded_at || 'entry'}-${index}`
          )
        )
      ]
    })
  })
}

function MemoryPane() {
  const scope = useScope()
  const history = useHistory(scope)
  const data = history.data
  const providerEntries = useProviderEntries(scope)
  const entries = [
    ...(Array.isArray(data?.entries) ? data.entries : []),
    ...providerEntries
  ]
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
  // Card triage: one fact at a time, centred and readable.
  deck: {
    display: 'grid',
    gap: 10,
    width: '100%',
    maxWidth: 720,
    boxSizing: 'border-box',
    margin: '0 auto',
    padding: 12
  },
  progress: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    flexWrap: 'wrap',
    fontSize: 10,
    color: 'var(--ui-text-tertiary)'
  },
  deckCard: {
    display: 'grid',
    gap: 10,
    padding: '14px 16px',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: 8,
    background: 'var(--ui-surface-background, var(--ui-bg-secondary))'
  },
  deckText: {
    fontSize: 14,
    lineHeight: 1.5,
    color: 'var(--ui-text-primary)',
    overflowWrap: 'anywhere'
  },
  deckMeta: {
    fontSize: 10,
    color: 'var(--ui-text-tertiary)',
    overflowWrap: 'anywhere'
  },
  deckActions: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    flexWrap: 'wrap',
    paddingTop: 2
  },
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

// The provenance line under a fact, shared by the list row and the card deck.
function reviewProvenance(item) {
  return item.date
    ? `${item.date}${item.context ? ' · ' + item.context : ''}`
    : (item.context ? item.context : 'source inconnue')
}

// Volatile hits live in the badge's title only; shared with the card deck.
function reviewVolatileTitle(item) {
  const hits =
    Array.isArray(item.volatile_hits) && item.volatile_hits.length
      ? item.volatile_hits.join(', ')
      : 'none'
  return `volatile: ${hits}`
}

// The staleness badges, shared by the list row and the card deck. `extra` is the
// row's per-fact actions (or its "derived" badge); the deck passes nothing.
function ReviewBadges({ item, verdict, extra }) {
  const invalid = item.state === 'invalidated'
  return jsxs('div', {
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
        title: reviewVolatileTitle(item),
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
      extra || null
    ]
  })
}

function ReviewRow({
  item,
  checked,
  onCheck,
  onAction,
  verdict,
  editing,
  editText,
  onEditText,
  onSaveEdit,
  onCancelEdit,
  onStartEdit
}) {
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
          editing
            ? jsxs('div', {
                style: { display: 'grid', gap: 6 },
                children: [
                  jsx('textarea', {
                    style: rv.textarea,
                    value: editText,
                    autoFocus: true,
                    onChange: e => onEditText(e.target.value),
                    onKeyDown: e => {
                      if (e.key === 'Escape') {
                        e.preventDefault()
                        onCancelEdit()
                      } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                        e.preventDefault()
                        onSaveEdit()
                      }
                    }
                  }),
                  jsxs('div', {
                    style: rv.actions,
                    children: [
                      jsx(Button, { type: 'button', size: 'xs', variant: 'primary', onClick: () => onSaveEdit(), children: 'Enregistrer' }),
                      jsx(Button, { type: 'button', size: 'xs', variant: 'ghost', onClick: () => onCancelEdit(), children: 'Annuler' })
                    ]
                  })
                ]
              })
            : jsx('div', {
                style: { ...rv.text, cursor: item.curatable ? 'pointer' : 'default' },
                onClick: () => { if (item.curatable) onStartEdit(item) },
                title: item.curatable ? 'Cliquer pour editer' : '',
                children: item.text
              }),
          jsx('div', {
            style: { fontSize: 10, color: 'var(--ui-text-tertiary)', overflowWrap: 'anywhere' },
            children: reviewProvenance(item)
          }),
          jsx(ReviewBadges, {
            item,
            verdict,
            extra: item.curatable
              ? jsxs('span', {
                  style: { ...rv.actions, marginLeft: 'auto' },
                  children: [
                    invalid
                      ? jsx(Button, { type: 'button', size: 'xs', variant: 'secondary', onClick: () => onAction('restore', item), children: 'Restore' })
                      : jsx(Button, { type: 'button', size: 'xs', variant: 'secondary', onClick: () => onAction('revoke', item), children: 'Revoke' })
                  ]
                })
              : jsx(Badge, { size: 'xs', variant: 'muted', children: 'derived' })
          })
        ]
      })
    ]
  })
}

// Card triage: one fact, its provenance and badges, and the three verdicts. The
// buttons mirror the keyboard shortcuts; the write itself lives in the page.
// "Correct" only writes the local ledger, so it stays available on any fact;
// only the revoke of "Faux" needs a curatable, valid fact.
function ReviewCard({ item, verdict, busy, onVerdict }) {
  const invalid = item.state === 'invalidated'
  const cannotRevoke = !item.curatable || invalid
  return jsxs('section', {
    style: rv.deckCard,
    children: [
      jsx('div', { style: rv.deckText, children: item.text }),
      jsx('div', { style: rv.deckMeta, children: reviewProvenance(item) }),
      jsx(ReviewBadges, { item, verdict }),
      jsxs('div', {
        style: rv.deckActions,
        children: [
          jsx(Button, {
            type: 'button', size: 'sm', variant: 'primary',
            disabled: busy,
            title: 'Marquer comme toujours vrai (aucune écriture dans Hindsight)',
            onClick: () => onVerdict('still true'),
            children: 'Correct'
          }),
          jsx(Button, {
            type: 'button', size: 'sm', variant: 'secondary',
            disabled: busy || cannotRevoke,
            title: cannotRevoke ? 'Fait dérivé ou déjà révoqué : non révocable' : 'Marquer comme faux et révoquer ce fait',
            onClick: () => onVerdict('outdated'),
            children: 'Faux'
          }),
          jsx(Button, {
            type: 'button', size: 'sm', variant: 'ghost',
            disabled: busy,
            title: 'Passer cette carte sans rien changer',
            onClick: () => onVerdict('cannot tell'),
            children: 'Je ne sais pas'
          })
        ]
      }),
      jsx('div', {
        style: rv.deckMeta,
        children: 'Raccourcis : → correct, ← faux, espace je ne sais pas'
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

const THEME_KEYS = [
  ['sante', 'Sante', ['sante', 'medecin', 'sommeil', 'traitement']],
  ['famille', 'Famille', ['ex-femme', 'parents', 'grand-mere', 'divorce']],
  ['argent', 'Argent', ['ynab', 'budget', 'salaire', 'banque', 'impots']],
  ['emploi', 'Emploi', ['emploi', 'cv', 'linkedin', 'entretien', 'recruteur', 'atlas']],
  ['infra', 'Infra', ['homelab', 'proxmox', 'oracle', 'zenith', 'zenon', 'kubernetes', 'ansible', 'tailscale', 'docker', 'dns', 'hermes', 'gateway']],
  ['projets', 'Projets', ['projet', 'plugin', 'hindsight', 'kanban', 'paseo', 'tronic', 'busybar', 'memoire']],
  ['preferences', 'Preferences', ['prefere', 'preference', 'aime', 'style', 'ton', 'langue', 'francais', 'falc']]
]
const ALLOWED_TAGS = new Set(['identite', 'travail', 'sante', 'famille', 'argent', 'preferences', 'projets', 'infra'])

function themeOf(item) {
  const tags = Array.isArray(item && item.tags) ? item.tags : []
  for (const t of tags) {
    const tag = String(t || '').trim().toLowerCase()
    if (ALLOWED_TAGS.has(tag)) {
      return tag.charAt(0).toUpperCase() + tag.slice(1)
    }
  }
  const haystack = (String((item && item.text) || '') + ' ' + String((item && item.context) || '')).toLowerCase()
  for (const [, label, words] of THEME_KEYS) {
    if (words.some(w => haystack.includes(w))) return label
  }
  if (haystack.includes('travail')) return 'Emploi'
  if (haystack.includes('identite')) return 'Identite'
  return 'General'
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
  const [showAdd, setShowAdd] = useState(false)
  const [newContent, setNewContent] = useState('')
  const [newContext, setNewContext] = useState('')
  const [newTags, setNewTags] = useState('')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [editingId, setEditingId] = useState(null)
  const [editText, setEditText] = useState('')
  const [view, setView] = useState('list')
  // Session-local bookkeeping for the card deck: ids that already left it (any
  // verdict), and whether the deck was refetched once after running out.
  const [skipped, setSkipped] = useState({})
  const [deckDone, setDeckDone] = useState(false)
  // In-flight guard for the deck: `busy` is read from this render's closure, so
  // two events before the re-render would both pass it. The ref flips synchronously.
  const decidingRef = useRef(false)
  const queryClient = useQueryClient()

  const cards = view === 'cards'
  const searchTerm = debouncedSearch
  const key = [ID, 'review', bank, state, offset, view, searchTerm]

  // A search runs over the whole bank server-side, so it must not fire on every
  // keystroke: the query key only sees the debounced value.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300)
    return () => clearTimeout(timer)
  }, [search])

  const query = useQuery({
    queryKey: key,
    queryFn: () =>
      reviewCall(
        cards
          ? `/review/${bank}/memories?limit=100&offset=0&state=valid&sort=staleness&review_only=1&q=${encodeURIComponent(searchTerm)}`
          : `/review/${bank}/memories?limit=100&offset=${offset}&state=${state}&sort=staleness&review_only=0&q=${encodeURIComponent(searchTerm)}`
      ),
    staleTime: 10000,
    retry: 1
  })

  const items = sortReview(Array.isArray(query.data?.items) ? query.data.items : [], sort)
  // The deck keeps the server's order (the queue is sorted by staleness there).
  const deckItems = cards && Array.isArray(query.data?.items) ? query.data.items : []
  // Session-local cursor: the first card the user has not decided on yet.
  const deckRemaining = deckItems.filter(i => !skipped[i.id])
  const card = deckRemaining[0] || null
  const picked = items.filter(i => selected[i.id])
  const refresh = () => void queryClient.invalidateQueries({ queryKey: [ID, 'review'] })

  // One verdict from the card deck: record the decision in the local ledger
  // first (no Hindsight write), then revoke for `outdated` only and drop the
  // card so the deck advances. An already-invalidated fact counts as revoked;
  // any other failure keeps the card so the user can retry it.
  const decide = async verdict => {
    if (decidingRef.current || !card) return
    if (verdict === 'outdated' && (!card.curatable || card.state === 'invalidated')) {
      setNotice('Fait dérivé ou déjà révoqué : il ne peut pas être marqué faux.')
      return
    }
    decidingRef.current = true
    setBusy(true)
    setNotice('')
    try {
      await reviewPost(`/review/${bank}/decisions`, {
        id: card.id,
        verdict,
        hash: card.text_hash
      })
      if (verdict === 'outdated') {
        try {
          await reviewPost(`/review/${bank}/memories/${encodeURIComponent(card.id)}/revoke`, {
            reason: 'outdated: marked in card review'
          })
        } catch (error) {
          const message = String((error && error.message) || error)
          if (!message.includes('not valid')) throw error
        }
      }
      setSkipped(prev => ({ ...prev, [card.id]: true }))
    } catch (error) {
      setNotice('Enregistrement impossible : ' + String((error && error.message) || error))
    } finally {
      decidingRef.current = false
      setBusy(false)
    }
  }

  // A new bank, view or search term starts a fresh deck. `state` and `sort` are
  // deliberately absent: both selects are list-only and the deck URL hardcodes
  // them, so they can only clear `skipped` without changing the query.
  useEffect(() => {
    setSkipped({})
    setDeckDone(false)
  }, [bank, view, searchTerm])

  // Deck exhausted: refetch once so the facts just decided leave and the next
  // batch of the queue loads.
  useEffect(() => {
    if (!cards || !query.data) return
    if (card || deckDone || query.isFetching) return
    setDeckDone(true)
    refresh()
  }, [cards, card, deckDone, query.isFetching, query.data])

  // Keyboard triage: arrows and space mirror the card's buttons. Auto-repeat is
  // filtered (holding ArrowRight would otherwise walk the whole queue) and the
  // focused control keeps its own keys: Space belongs to a focused button.
  useEffect(() => {
    if (!cards || !card || busy) return undefined
    const onKeyDown = event => {
      if (event.repeat) return
      const target = event.target
      if (
        target &&
        target.closest &&
        target.closest('input, textarea, select, button, a, [role="button"], [contenteditable]')
      ) {
        return
      }
      if (document.querySelector('[role="dialog"]')) return
      const key = event.key
      if (key === 'ArrowRight') decide('still true')
      else if (key === 'ArrowLeft') decide('outdated')
      else if (key === ' ' || key === 'Spacebar') decide('cannot tell')
      else return
      event.preventDefault()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [cards, card, busy])

  const submitAdd = async () => {
    const content = newContent.trim()
    if (!content) {
      setNotice('Le contenu est requis.')
      return
    }
    const tags = newTags.split(',').map(t => t.trim()).filter(Boolean)
    setBusy(true)
    setNotice('')
    try {
      const result = await reviewPost(`/review/${bank}/memories`, {
        content,
        context: newContext.trim(),
        tags
      })
      setNewContent('')
      setNewContext('')
      setNewTags('')
      setShowAdd(false)
      const asyncFlag = result && result.created && result.created.async
      setNotice(
        asyncFlag
          ? 'Souvenir enregistre. L extraction continue en arriere-plan.'
          : 'Souvenir enregistre.'
      )
      refresh()
    } catch (error) {
      setNotice(`Ajout impossible : ${String(error && error.message ? error.message : error)}`)
    } finally {
      setBusy(false)
    }
  }

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
      setNotice(`Could not start the check: ${String(error && error.message ? error.message : error)}`)
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
      setNotice(`Could not read verdicts: ${String(error && error.message ? error.message : error)}`)
    } finally {
      setBusy(false)
    }
  }

  const startEdit = item => {
    setEditingId(item.id)
    setEditText(item.text || '')
  }
  const cancelEdit = () => {
    setEditingId(null)
    setEditText('')
  }
  const saveEdit = async () => {
    if (editingId == null) return
    const item = items.find(i => i.id === editingId)
    if (!item) { cancelEdit(); return }
    const text = editText.trim()
    if (!text) { setNotice('Le texte est requis.'); return }
    setBusy(true)
    setNotice('')
    try {
      const base = `/review/${bank}/memories/${encodeURIComponent(item.id)}`
      const result = await reviewPost(`${base}/update`, { text })
      setLast({ kind: 'update', bank, previous: result.previous, current: result.current })
      setNotice('Souvenir mis a jour.')
      setEditingId(null)
      setEditText('')
      refresh()
    } catch (error) {
      setNotice(`Mise a jour impossible : ${String(error && error.message ? error.message : error)}`)
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

  // List mode scans the same bounded window as the deck: say so once, in the
  // same words, instead of letting an empty page look like an empty bank.
  const truncatedNote = query.data?.truncated
    ? jsx('div', {
        style: rv.progress,
        children: 'La banque est plus grande que la fenêtre analysée : certains souvenirs ne sont pas dans cette file.'
      })
    : null

  // group by theme
  const groups = new Map()
  for (const it of items) {
    const t = themeOf(it)
    if (!groups.has(t)) groups.set(t, [])
    groups.get(t).push(it)
  }
  let finalGroups = groups
  if (groups.size > 8) {
    finalGroups = new Map()
    const autres = []
    for (const [t, its] of groups) {
      if (t !== 'General' && its.length <= 1) autres.push(...its)
      else finalGroups.set(t, its)
    }
    if (autres.length) finalGroups.set('Autres', autres)
  }
  const themeKeys = [...finalGroups.keys()].sort((a, b) => {
    if (a === 'General') return 1
    if (b === 'General') return -1
    return String(a).localeCompare(String(b))
  })

  return jsxs('div', {
    style: rv.page,
    children: [
      jsxs('header', {
        style: rv.bar,
        children: [
          jsx('div', { style: { ...styles.title, flex: 1 }, children: 'Memory review' }),
          jsx(Button, {
            type: 'button', size: 'xs', variant: cards ? 'primary' : 'secondary',
            title: cards ? 'Revenir à la liste' : 'Trier les souvenirs carte par carte',
            disabled: !cards && (query.data?.to_review || 0) === 0,
            onClick: () => { setView(cards ? 'list' : 'cards'); setOffset(0) },
            children: cards ? 'Liste' : 'Cartes'
          }),
          select(bank, v => { setBank(v); setOffset(0); setSelected({}) }, REVIEW_BANKS.map(b => [b, b])),
          // `state` and `sort` are list-only: the deck's URL hardcodes both.
          cards
            ? null
            : select(state, v => { setState(v); setOffset(0) }, [['valid', 'valid'], ['invalidated', 'revoked']]),
          cards
            ? null
            : select(sort, setSort, [['staleness', 'Sort: staleness'], ['age', 'Sort: age'], ['volatile', 'Sort: volatile']]),
          jsx(Input, {
            style: { ...rv.field, minWidth: 160, flex: 1 },
            value: search,
            placeholder: 'Rechercher dans la banque',
            onChange: e => setSearch(e.target.value)
          }),
          jsx('span', {
            style: styles.footerText,
            title: 'Faits de la page affichée, faits analysés dans la fenêtre de scan, et file "à revoir" de toute la banque.',
            children: `${items.length} affichés · ${query.data?.matched ?? 0} analysés · ${query.data?.to_review ?? 0} à revoir`
          }),
          jsx(Button, {
            type: 'button', size: 'xs', variant: 'secondary',
            disabled: busy || picked.length === 0 || picked.length > REVIEW_MAX_CHECK,
            onClick: () => void runCheck(),
            children: `Check with Hermes (${picked.length})`
          }),
          jsx(Button, {
            type: 'button', size: 'xs', variant: 'secondary',
            onClick: () => setShowAdd(v => !v),
            children: 'Ajouter un souvenir'
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
      showAdd
        ? jsxs('div', {
            style: { ...styles.note, margin: '8px 12px 0', display: 'grid', gap: 8 },
            children: [
              jsx(Textarea, { style: rv.textarea, value: newContent, placeholder: 'Le fait a memoriser', onChange: e => setNewContent(e.target.value) }),
              jsx(Input, { style: { ...rv.field, width: '100%', boxSizing: 'border-box' }, value: newContext, placeholder: 'Contexte (facultatif)', onChange: e => setNewContext(e.target.value) }),
              jsx(Input, { style: { ...rv.field, width: '100%', boxSizing: 'border-box' }, value: newTags, placeholder: 'tags separes par des virgules', onChange: e => setNewTags(e.target.value) }),
              jsxs('div', {
                style: rv.actions,
                children: [
                  jsx(Button, { type: 'button', size: 'sm', variant: 'primary', disabled: busy || !newContent.trim(), onClick: () => void submitAdd(), children: 'Ajouter' }),
                  jsx(Button, { type: 'button', size: 'sm', variant: 'ghost', disabled: busy, onClick: () => setShowAdd(false), children: 'Annuler' })
                ]
              })
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
            : cards
              ? jsx(ScrollArea, {
                  style: { flex: 1, minHeight: 0 },
                  children: jsxs('div', {
                    style: rv.deck,
                    children: [
                      card
                        ? jsx('div', {
                            style: rv.progress,
                            children: `carte ${deckItems.indexOf(card) + 1} / ${deckItems.length}`
                          })
                        : null,
                      query.data?.truncated
                        ? jsx('div', {
                            style: rv.progress,
                            children: 'La banque est plus grande que la fenêtre analysée : certains souvenirs ne sont pas dans cette file.'
                          })
                        : null,
                      card
                        ? jsx(ReviewCard, { item: card, verdict: verdicts[card.id], busy, onVerdict: decide })
                        : (query.data?.to_review ?? 0) === 0
                          ? jsx(EmptyState, {
                              title: 'Rien à revoir',
                              description: 'Aucun souvenir de cette banque ne demande de revue.'
                            })
                          : jsxs('div', {
                              style: { ...styles.centered, gap: 10, padding: 16 },
                              children: [
                                jsx(EmptyState, {
                                  title: 'File terminée',
                                  description: 'Toutes les cartes de cette file ont reçu un verdict.'
                                }),
                                jsx(Button, {
                                  type: 'button', size: 'sm', variant: 'secondary',
                                  disabled: query.isFetching, onClick: refresh, children: 'Rafraîchir'
                                })
                              ]
                            })
                    ]
                  })
                })
              : items.length === 0
              ? jsxs('div', {
                  style: { ...styles.centered, gap: 10 },
                  children: [
                    truncatedNote,
                    jsx(EmptyState, { title: 'No facts here', description: 'Nothing to review in this bank and state.' }),
                    // Past the scan window the page is empty but the offset is not 0:
                    // keep a way back instead of a dead end.
                    offset > 0
                      ? jsx(Button, { type: 'button', size: 'xs', variant: 'ghost', onClick: () => setOffset(Math.max(0, offset - 100)), children: 'Newer' })
                      : null
                  ]
                })
              : jsx(ScrollArea, {
                  style: { flex: 1, minHeight: 0 },
                  children: jsxs('div', {
                    style: styles.list,
                    children: [
                      truncatedNote,
                      ...themeKeys.map(theme =>
                        jsxs('div', {
                          key: theme,
                          style: { display: 'grid', gap: 6 },
                          children: [
                            jsxs('div', {
                              style: { fontSize: 12, fontWeight: 600, color: 'var(--ui-text-secondary)', padding: '4px 2px' },
                              children: [theme, jsx('span', { style: { marginLeft: 6, fontSize: 10, color: 'var(--ui-text-tertiary)' }, children: `(${finalGroups.get(theme).length})` })]
                            }),
                            ...finalGroups.get(theme).map(item =>
                              jsx(ReviewRow, {
                                key: item.id, item, checked: Boolean(selected[item.id]), verdict: verdicts[item.id],
                                editing: editingId === item.id,
                                editText: editingId === item.id ? editText : '',
                                onEditText: setEditText,
                                onSaveEdit: saveEdit,
                                onCancelEdit: cancelEdit,
                                onStartEdit: startEdit,
                                onCheck, onAction: (kind, it) => setAction({ kind, item: it, bank })
                              })
                            )
                          ]
                        }, theme)
                      ),
                      jsxs('div', {
                        style: rv.actions,
                        children: [
                          jsx(Button, { type: 'button', size: 'xs', variant: 'ghost', disabled: offset === 0, onClick: () => setOffset(Math.max(0, offset - 100)), children: 'Newer' }),
                          jsx('span', { style: styles.footerText, children: `${offset + 1}-${offset + items.length} of ${formatNumber(query.data?.total)}` }),
                          jsx(Button, {
                            type: 'button', size: 'xs', variant: 'ghost',
                            // Stop at the end of the page slice or of the scored set: past
                            // the scan window the backend returns an empty page.
                            disabled: items.length < 100 || offset + 100 >= (query.data?.matched ?? 0),
                            onClick: () => setOffset(offset + 100),
                            children: 'Older'
                          })
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

    ctx.onEvent('status.update', event => {
      const sid =
        (event && (event.session_id || (event.params && event.params.session_id))) || ''
      const payload =
        (event && (event.payload || (event.params && event.params.payload))) || {}
      const text = payload && payload.text ? String(payload.text) : ''
      if (!sid || !/recalled|Hindsight/i.test(text)) return
      const entry = {
        id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        session_id: sid,
        recorded_at: new Date().toISOString(),
        text,
        bank: null,
        injected_chars: null,
        status: 'injected',
        platform: ''
      }
      providerRecalls.set(providerRecalls.get().concat([entry]))
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
