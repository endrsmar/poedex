/**
 * The `full` profile: web, unconstrained, pointer and keyboard, real CSS.
 *
 * Selected by the absence of the `poedex-compact` export condition — see
 * `ui-kit/src/index.ts`. The stylesheet is imported here and nowhere else, so the
 * `compact` bundle never sees it.
 */

import './theme.css'
import type { KitImplementation } from '../../contracts'
import { FULL_PROFILE } from '../../profile'

export { Screen, Section, Stack, Row, Focus } from './layout'
export { Stat, Tally, ItemGrid, ItemRow, ValueBar, VerdictPill } from './data'
export { Action, CheckList, Stepper, Detail } from './interaction'
export { Pending, Empty, ErrorState, StaleBanner } from './state'

export const PROFILE = FULL_PROFILE

import * as layout from './layout'
import * as data from './data'
import * as interaction from './interaction'
import * as state from './state'

/**
 * Compile-time proof that this profile implements every primitive with the
 * contract's signature. `compact` has the identical assertion, which is what makes
 * "designed against both profiles" checkable rather than claimed.
 */
const _implementation: KitImplementation = {
  Screen: layout.Screen,
  Section: layout.Section,
  Stack: layout.Stack,
  Row: layout.Row,
  Focus: layout.Focus,
  Stat: data.Stat,
  Tally: data.Tally,
  ItemGrid: data.ItemGrid,
  ItemRow: data.ItemRow,
  ValueBar: data.ValueBar,
  VerdictPill: data.VerdictPill,
  Action: interaction.Action,
  CheckList: interaction.CheckList,
  Stepper: interaction.Stepper,
  Detail: interaction.Detail,
  Pending: state.Pending,
  Empty: state.Empty,
  ErrorState: state.ErrorState,
  StaleBanner: state.StaleBanner,
}
void _implementation
