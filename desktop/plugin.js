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
  CopyButton,
  EmptyState,
  ErrorState,
  Loader,
  PALETTE_AREA,
  PANES_AREA,
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

export default {
  id: ID,
  name: 'Hindsight Memory',
  description:
    'Shows the Hindsight mental-model context injected into the focused chat.',
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
