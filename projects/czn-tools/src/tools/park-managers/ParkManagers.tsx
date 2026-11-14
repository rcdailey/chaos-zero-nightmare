import { useEffect, useId, useMemo, useState } from 'react'
import {
  ATTRACTIONS,
  MANAGERS,
  STATS,
  activeEffects,
  avatarUrl,
  bestRoster,
  isUnlocked,
  points,
  type Effect,
  type Manager,
  type Park,
} from './roster'

const STORAGE_KEY = 'czn-manager-optimizer:park'
const DEFAULT_PARK: Park = {
  rank: 1,
  slots: 8,
  stats: { thrill: 0, comfort: 0, happiness: 0 },
  attractions: [],
}

/** Reads saved progress, falling back to defaults for anything missing or malformed. */
function loadPark(): Park {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as Partial<Park>
    const int = (v: unknown, min: number, fallback: number, max = Infinity) =>
      Number.isInteger(v) && (v as number) >= min && (v as number) <= max
        ? (v as number)
        : fallback
    return {
      rank: int(saved.rank, 1, DEFAULT_PARK.rank),
      slots: int(saved.slots, 1, DEFAULT_PARK.slots, MANAGERS.length),
      stats: Object.fromEntries(
        STATS.map((s) => [s, int(saved.stats?.[s], 0, 0)]),
      ) as Park['stats'],
      attractions: Array.isArray(saved.attractions)
        ? saved.attractions.filter((a) => ATTRACTIONS.includes(a))
        : [],
    }
  } catch {
    return DEFAULT_PARK
  }
}

const HISTORY_KEY = 'czn-manager-optimizer:history'
const HISTORY_LIMIT = 30
// Typing "361" passes through 3 and 36; waiting for input to settle records only the net change.
const SETTLE_MS = 800
const MERGE_MS = 60_000

type Change = { at: number; added: string[]; removed: string[] }
/** Roster changes, newest first, relative to `roster`, the last recorded recommendation. */
type History = { roster: string[]; changes: Change[] }

function loadHistory(current: string[]): History {
  const names = (v: unknown) =>
    Array.isArray(v) ? v.filter((n) => MANAGERS.some((m) => m.name === n)) : []
  try {
    const saved = JSON.parse(localStorage.getItem(HISTORY_KEY) ?? 'null')
    if (!Array.isArray(saved?.roster)) return { roster: current, changes: [] }
    const changes = Array.isArray(saved.changes) ? saved.changes : []
    return {
      roster: names(saved.roster),
      changes: changes
        .filter((c: Partial<Change>) => typeof c?.at === 'number')
        .map((c: Change) => ({ at: c.at, added: names(c.added), removed: names(c.removed) }))
        .slice(0, HISTORY_LIMIT),
    }
  } catch {
    return { roster: current, changes: [] }
  }
}

/**
 * Folds a roster change into the newest entry when that entry was updated within MERGE_MS,
 * keeping only the net change; an entry that nets to nothing is dropped.
 */
function record(history: History, roster: string[]): History {
  const diff = (from: string[]) => ({
    added: roster.filter((n) => !from.includes(n)),
    removed: from.filter((n) => !roster.includes(n)),
  })
  const change = diff(history.roster)
  if (!change.added.length && !change.removed.length) return history
  const at = Date.now()
  const [latest, ...older] = history.changes
  if (!latest || at - latest.at > MERGE_MS) {
    return { roster, changes: [{ at, ...change }, ...history.changes].slice(0, HISTORY_LIMIT) }
  }
  const rosterBeforeLatest = [
    ...history.roster.filter((n) => !latest.added.includes(n)),
    ...latest.removed,
  ]
  const merged = diff(rosterBeforeLatest)
  if (!merged.added.length && !merged.removed.length) return { roster, changes: older }
  return { roster, changes: [{ at, ...merged }, ...older] }
}

const byName = new Map(MANAGERS.map((m) => [m.name, m]))

function describe(e: Effect): string {
  const stat = e.stat ? `${e.stat[0].toUpperCase()}${e.stat.slice(1)} ${e.min}+` : undefined
  return [e.partner, e.attraction, stat].filter(Boolean).join(' + ')
}

function Avatar({ manager, size, dim }: { manager: Manager; size: number; dim?: boolean }) {
  return (
    <div
      className={`shrink-0 rounded-full bg-nord-2 ring-nord-8/50 shadow-lg ${
        size < 50 ? 'ring-2' : 'ring-3'
      } ${
        dim ? 'grayscale brightness-50' : ''
      }`}
      style={{
        width: size,
        height: size,
        backgroundImage: `url('${avatarUrl(manager)}')`,
        backgroundPosition: '50% 8%',
        backgroundSize: '175%',
        backgroundRepeat: 'no-repeat',
      }}
    />
  )
}

function Stepper(props: {
  label: string
  value: number
  step: number
  min: number
  onChange: (value: number) => void
}) {
  const { label, value, step, min, onChange } = props
  const id = useId()
  // Raw text while the user types, so the field can be cleared before entering a new number.
  const [draft, setDraft] = useState<string | null>(null)
  const set = (v: number) => onChange(Math.max(min, Math.round(v)))
  const stepBy = (delta: number) => {
    setDraft(null)
    set(value + delta)
  }
  // Buttons are skipped by Tab (tabIndex -1) so Tab moves straight between fields.
  const button =
    'size-8 rounded-full text-xl leading-none text-nord-8 hover:bg-nord-2 active:bg-nord-3'
  return (
    <div className="flex items-center gap-2">
      <label htmlFor={id} className="w-24 text-nord-4">{label}</label>
      <button type="button" tabIndex={-1} aria-label={`Decrease ${label}`} className={button}
        onClick={() => stepBy(-step)}>
        −
      </button>
      <input
        id={id}
        type="number"
        inputMode="numeric"
        min={min}
        value={draft ?? value}
        onChange={(e) => {
          setDraft(e.target.value)
          if (Number.isFinite(e.target.valueAsNumber)) set(e.target.valueAsNumber)
        }}
        onBlur={() => setDraft(null)}
        onDoubleClick={(e) => e.currentTarget.select()}
        className="w-20 rounded-lg border border-nord-3 bg-nord-0 px-2 py-1.5 text-center
          text-nord-6 [appearance:textfield] focus:border-nord-8 focus:outline-none
          [&::-webkit-inner-spin-button]:appearance-none"
      />
      <button type="button" tabIndex={-1} aria-label={`Increase ${label}`} className={button}
        onClick={() => stepBy(step)}>
        +
      </button>
    </div>
  )
}

function ParkPanel({ park, setPark }: { park: Park; setPark: (p: Park) => void }) {
  const toggle = (name: string) =>
    setPark({
      ...park,
      attractions: park.attractions.includes(name)
        ? park.attractions.filter((a) => a !== name)
        : [...park.attractions, name],
    })
  return (
    <section className="flex shrink-0 flex-col gap-3 rounded-2xl border border-nord-3
      bg-nord-1 p-5">
      <h2 className="text-xl font-bold text-nord-6">Park</h2>
      <Stepper label="Rank" value={park.rank} step={1} min={1}
        onChange={(rank) => setPark({ ...park, rank })} />
      <Stepper label="Slots" value={park.slots} step={1} min={1}
        onChange={(slots) => setPark({ ...park, slots })} />
      <hr className="border-nord-3" />
      {STATS.map((stat) => (
        <Stepper key={stat} label={stat[0].toUpperCase() + stat.slice(1)}
          value={park.stats[stat]} step={10} min={0}
          onChange={(v) => setPark({ ...park, stats: { ...park.stats, [stat]: v } })} />
      ))}
      <hr className="border-nord-3" />
      <h3 className="text-nord-4">Attractions</h3>
      <div className="flex flex-wrap gap-1.5">
        {ATTRACTIONS.map((name) => {
          const owned = park.attractions.includes(name)
          return (
            <button key={name} type="button" aria-pressed={owned} onClick={() => toggle(name)}
              className={`rounded-full border px-2.5 py-0.5 text-sm transition ${
                owned
                  ? 'border-nord-8 bg-nord-8 text-nord-0'
                  : 'border-nord-3 text-nord-4/60 hover:border-nord-8'
              }`}>
              <span aria-hidden className={owned ? undefined : 'invisible'}>✓ </span>
              {name}
            </button>
          )
        })}
      </div>
    </section>
  )
}

function RosterCard({ manager, deployed, park }: {
  manager: Manager
  deployed: Set<string>
  park: Park
}) {
  const value = points(manager, deployed, park)
  return (
    <article className="flex flex-col items-center gap-1 rounded-2xl border border-nord-3
      bg-nord-1 p-4 text-center">
      <Avatar manager={manager} size={96} />
      <span className="mt-2 rounded-full bg-nord-2 px-3 py-0.5 font-semibold
        text-nord-6">
        {manager.name}
      </span>
      <span className="text-sm text-nord-4/70">{value} / {manager.interval}s</span>
      <span className="text-xl font-bold text-nord-13">
        {(value / manager.interval).toFixed(1)}/s
      </span>
      {activeEffects(manager, deployed, park).map((e) => (
        <span key={describe(e)} className="text-xs text-nord-14">
          +{e.bonus} {describe(e)}
        </span>
      ))}
    </article>
  )
}

function HistoryPanel({ history, onClear }: { history: History; onClear: () => void }) {
  const chip = (name: string, sign: '+' | '-') => (
    <span key={sign + name} className={`flex items-center gap-1.5 rounded-full py-0.5 pr-2.5
      pl-0.5 text-sm font-medium ${
      sign === '+' ? 'bg-nord-14/20 text-nord-14' : 'bg-nord-11/20 text-nord-11'
    }`}>
      <Avatar manager={byName.get(name)!} size={44} />
      {sign} {name}
    </span>
  )
  return (
    <section className="flex min-h-0 flex-1 flex-col gap-2 rounded-2xl border border-nord-3
      bg-nord-1 p-4">
      <div className="flex items-baseline gap-3">
        <h2 className="text-lg font-bold text-nord-6">Changes</h2>
        {history.changes.length > 0 && (
          <button type="button" onClick={onClear}
            className="text-xs text-nord-4/60 hover:text-nord-8">
            Clear
          </button>
        )}
      </div>
      {history.changes.length === 0 && (
        <p className="text-sm text-nord-4/60">
          Roster changes appear here when your park changes the recommendation.
        </p>
      )}
      <ol className="-mr-2 flex min-h-0 flex-col gap-1.5 overflow-y-auto pr-2">
        {history.changes.map((c, i) => (
          <li key={c.at} className={`flex flex-wrap items-center gap-1.5 rounded-lg border p-2
            ${i === 0 ? 'border-nord-8/60 bg-nord-0' : 'border-nord-2 opacity-70'}`}>
            <time className="w-full text-xs text-nord-4/60">
              {new Date(c.at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
            </time>
            {c.removed.map((n) => chip(n, '-'))}
            {c.added.map((n) => chip(n, '+'))}
          </li>
        ))}
      </ol>
    </section>
  )
}

export default function ParkManagers() {
  const [park, setPark] = useState(loadPark)
  useEffect(() => localStorage.setItem(STORAGE_KEY, JSON.stringify(park)), [park])

  const roster = useMemo(() => bestRoster(park), [park])
  const [history, setHistory] = useState(() => loadHistory(roster))
  useEffect(() => {
    const timer = setTimeout(() => setHistory((h) => record(h, roster)), SETTLE_MS)
    return () => clearTimeout(timer)
  }, [roster])
  useEffect(() => localStorage.setItem(HISTORY_KEY, JSON.stringify(history)), [history])
  const deployed = new Set(roster)
  const rate = (m: Manager) => points(m, deployed, park) / m.interval
  const cards = roster.map((n) => byName.get(n)!).sort((a, b) => rate(b) - rate(a))
  const total = cards.reduce((sum, m) => sum + rate(m), 0)

  return (
    <div className="flex flex-col items-start gap-6 lg:flex-row">
      <div className="flex w-full shrink-0 flex-col gap-4 lg:sticky lg:top-6 lg:h-[calc(100vh-3rem)]
        lg:w-80">
        <ParkPanel park={park} setPark={setPark} />
        <HistoryPanel history={history}
          onClear={() => setHistory({ roster, changes: [] })} />
      </div>
      <div className="flex w-full flex-col gap-4">
        <header className="flex items-baseline gap-3">
          <span className="text-5xl font-bold text-nord-13">{total.toFixed(1)}</span>
          <span className="text-lg text-nord-4">Management Profit / s</span>
        </header>
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          {cards.map((m) => (
            <RosterCard key={m.name} manager={m} deployed={deployed} park={park} />
          ))}
        </div>
        <h2 className="mt-4 text-lg text-nord-4">Bench</h2>
        <div className="flex flex-wrap gap-4">
          {MANAGERS.filter((m) => !deployed.has(m.name)).map((m) => {
            const locked = !isUnlocked(m, park)
            return (
              <div key={m.name} className="flex w-20 flex-col items-center gap-1 text-xs">
                <Avatar manager={m} size={56} dim={locked} />
                <span className="text-nord-5">{m.name}</span>
                <span className={locked ? 'text-nord-11' : 'text-nord-4/50'}>
                  {locked ? `Rank ${m.unlock_rank}` : `${m.base}/${m.interval}s`}
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
