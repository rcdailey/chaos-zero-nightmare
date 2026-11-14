import type { ComponentType } from 'react'
import ParkManagers from './park-managers/ParkManagers'

export interface Tool {
  /** URL segment after `#/`; keep stable, since visitors bookmark and share it. */
  path: string
  name: string
  component: ComponentType
}

/** Every published tool, in sidebar order. Adding a tool here adds its route and nav item. */
export const TOOLS: Tool[] = [
  { path: 'park-managers', name: 'Park Managers', component: ParkManagers },
]
