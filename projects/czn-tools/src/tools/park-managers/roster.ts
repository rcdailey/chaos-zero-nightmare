import data from './managers.json'

/**
 * Manager profit model, verified against in-game totals.
 *
 * A manager earns (base + active bonuses) points every `interval` seconds. A bonus is active when
 * every condition it lists holds: the partner is also deployed as a manager, the attraction is
 * owned, and the park stat is at or above `min`.
 */

export const STATS = ['thrill', 'comfort', 'happiness'] as const
export type Stat = (typeof STATS)[number]

export interface Effect {
  bonus: number
  partner?: string
  attraction?: string
  stat?: Stat
  min?: number
}

export interface Manager {
  name: string
  base: number
  interval: number
  unlock_rank?: number
  avatar?: string
  effects: Effect[]
}

/** Player progress; persisted in the browser, never in the game data. */
export interface Park {
  rank: number
  slots: number
  stats: Record<Stat, number>
  attractions: string[]
}

export const ATTRACTIONS: string[] = data.attractions
export const MANAGERS = data.managers as Manager[]

const AVATAR_CDN = 'https://cdn.prydwen.gg/images/chaos/characters/'

export function avatarUrl(m: Manager): string {
  return AVATAR_CDN + (m.avatar ?? `${m.name.toLowerCase()}_card.webp`)
}

export function isUnlocked(m: Manager, park: Park): boolean {
  return (m.unlock_rank ?? 0) <= park.rank
}

export function activeEffects(m: Manager, roster: Set<string>, park: Park): Effect[] {
  return m.effects.filter(
    (e) =>
      (e.partner === undefined || roster.has(e.partner)) &&
      (e.attraction === undefined || park.attractions.includes(e.attraction)) &&
      (e.stat === undefined || park.stats[e.stat] >= (e.min ?? 0)),
  )
}

export function points(m: Manager, roster: Set<string>, park: Park): number {
  return activeEffects(m, roster, park).reduce((sum, e) => sum + e.bonus, m.base)
}

/**
 * Branch-and-bound over all rosters of size `park.slots`.
 *
 * The total rate is a sum of per-manager terms (`solo`) plus pairwise partner terms (`link`), so
 * adding a manager to a partial roster has a cheap exact gain. Candidates are sorted by an
 * optimistic per-manager ceiling, so the next r ceilings bound any completion of the roster.
 */
export function bestRoster(park: Park): string[] {
  const candidates = MANAGERS.filter((m) => isUnlocked(m, park))
  const everyone = new Set(candidates.map((m) => m.name))
  const index = new Map(candidates.map((m, i) => [m.name, i]))
  const n = candidates.length
  const solo = Array.from({ length: n }, () => 0)
  const link = candidates.map(() => Array.from({ length: n }, () => 0))

  candidates.forEach((m, i) => {
    solo[i] += m.base / m.interval
    for (const e of activeEffects(m, everyone, park)) {
      const rate = e.bonus / m.interval
      const j = e.partner === undefined ? undefined : index.get(e.partner)
      if (j === undefined) {
        solo[i] += rate
      } else {
        link[i][j] += rate
        link[j][i] += rate
      }
    }
  })

  const ceiling = solo.map((s, i) => s + link[i].reduce((a, b) => a + b, 0))
  const order = [...ceiling.keys()].toSorted((a, b) => ceiling[b] - ceiling[a])
  const size = Math.min(park.slots, n)
  let bestRate = -1
  let best: number[] = []
  const chosen: number[] = []

  const search = (start: number, rate: number) => {
    const remaining = size - chosen.length
    if (remaining === 0) {
      if (rate > bestRate) {
        bestRate = rate
        best = [...chosen]
      }
      return
    }
    for (let pos = start; pos <= n - remaining; pos++) {
      let bound = rate
      for (let p = pos; p < pos + remaining; p++) bound += ceiling[order[p]]
      if (bound <= bestRate) return
      const i = order[pos]
      const gain = chosen.reduce((g, j) => g + link[i][j], solo[i])
      chosen.push(i)
      search(pos + 1, rate + gain)
      chosen.pop()
    }
  }

  search(0, 0)
  return best.map((i) => candidates[i].name)
}
