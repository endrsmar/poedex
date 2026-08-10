/**
 * The primitive contracts (IMPLEMENTATION-PLAN §2.3).
 *
 * **Every contract in this file was written against both profiles before either
 * was implemented.** That is Phase 5's stated risk and this file is the mitigation,
 * so the rules it follows are worth stating, because they are what make a `compact`
 * implementation possible rather than merely promised:
 *
 * 1. **No escape hatches.** No `className`, no `style`, no `ref`, no `as`. Under
 *    `compact` those are meaningless (`@decky/ui`, inline styles, Steam's React) and
 *    a contract that carries them is a contract only `full` can honour. This is the
 *    single most load-bearing rule here.
 * 2. **`children` is only ever other primitives.** Anywhere a module would want to
 *    hand over arbitrary markup, the contract takes a *model* instead — `ItemRow`
 *    takes `ItemRowModel`, not a render function. A render prop is a hole through
 *    which raw DOM reaches a surface that has none.
 * 3. **Density is declared, never branched on.** Anything that differs by profile is
 *    a `Hint<T>` prop. There is no `useProfile()` in a module.
 * 4. **Navigation belongs to the kit.** `ItemGrid` and `ItemRow` take `onSelect`,
 *    never `onKeyDown` / `onGamepadFocus` / `tabIndex`. §2.3: the module never
 *    imports `Focusable` or writes a focus handler.
 * 5. **Nothing is hidden without saying how much.** `limit` truncates and reports.
 *    That is a `compact` need — five rows in 300 px — but it is honest at any width,
 *    which is how you can tell it is a real contract rather than a `compact` special
 *    case bolted on.
 *
 * Where a contract has a deliberate cost at one density, it is said out loud on the
 * prop. Phase 7 is allowed to revise these; it is not allowed to bury the revision
 * under overrides.
 */

import type { ReactElement, ReactNode } from 'react'
import type { Hint } from '../profile'
import type {
  BarModel,
  CheckField,
  CheckOptionModel,
  DetailField,
  DetailModel,
  GridCellModel,
  ItemRowModel,
  SyncStatus,
  TallyEntry,
  Tone,
  Verdict,
} from './model'

export type * from './model'

/** Slot for other primitives. Typed as `ReactNode` because React demands it; the
 * rule that it holds primitives and not markup is enforced by the lint rule over
 * `modules/<id>/ui`, which forbids a module importing anything that could produce
 * raw DOM in the first place. */
type Slot = ReactNode

// ---------------------------------------------------------------- layout ----

export interface ScreenProps {
  id: string
  title: string
  /** One line under the title. Dropped entirely at `compact`. */
  subtitle?: string
  /**
   * Sync state, warnings — anything that must stay visible while the body
   * scrolls. `compact` pins it under the panel header; `full` pins it under the
   * page header. Not `children`, because a banner that scrolls away is a banner
   * that is not doing its job.
   */
  banner?: Slot
  /** `Action`s for the screen as a whole. `compact` renders them as the QAM footer
   * hint row (`A Details · Y Resync`); `full` as buttons beside the title. */
  actions?: Slot
  /**
   * Secondary content: at `full` a right-hand rail, at `compact` a strip below the
   * body. One JSX tree, two layouts — this prop is the whole reason the bag screen
   * does not need `overrides/compact.tsx` for its detail pane.
   */
  aside?: Slot
  children: Slot
}

export interface SectionProps {
  /** Mono, uppercase, 0.12em tracking at both densities. */
  title?: string
  /** Right-hand side of the title line: a count, a subtotal, `by value`. */
  meta?: string
  /** A sentence under the title. `compact` shows it only when `tone` is not quiet. */
  description?: string
  tone?: Tone
  /**
   * How many children to render before truncating. `null` means all.
   * `{ compact: 5, full: null }` is the canonical use, and the hidden count is
   * always reported — see rule 5 in this file's docstring.
   */
  limit?: Hint<number | null>
  /** Word used in the "N more" footer: `rows`, `items`, `listings`. */
  limitNoun?: string
  /** Pressing the footer asks the caller to raise `limit`. Without it the footer
   * is a statement rather than a control, which is the right default at `compact`
   * where a press costs a focus stop. */
  onShowAll?: () => void
  collapsible?: Hint<boolean>
  defaultCollapsed?: Hint<boolean>
  children: Slot
}

export interface StackProps {
  gap?: Hint<'none' | 'xs' | 'sm' | 'md' | 'lg'>
  children: Slot
}

export interface RowProps {
  gap?: Hint<'none' | 'xs' | 'sm' | 'md' | 'lg'>
  align?: 'start' | 'center' | 'baseline' | 'end'
  justify?: 'start' | 'between' | 'end'
  /** `compact` wraps by default: 300 px turns a four-item row into a scrollbar. */
  wrap?: Hint<boolean>
  children: Slot
}

// ------------------------------------------------------------------ data ----

export interface StatProps {
  label: string
  /** Pre-formatted. The kit will not guess at locale, precision or currency. */
  value: string
  unit?: string
  /**
   * Rendered immediately before the value, tight. This exists so `≥` is a field
   * rather than string concatenation: SPEC §5.3 requires the bag total to read
   * `≥ N div` while tier 3 is outstanding, and at `compact` the prefix wants to be
   * a superscript rather than a character stealing 8 px from a 26 px number.
   */
  prefix?: string
  /** A second figure under the first: `2.04 div` under `1,824c`. */
  secondary?: string
  /** Why the number is what it is. This is where "excludes 3 unpriceable rows —
   * the total is a floor, not a value" goes, and it is not optional decoration. */
  note?: string
  tone?: Tone
  size?: Hint<'sm' | 'md' | 'lg'>
}

export interface TallyProps {
  entries: TallyEntry[]
  /** Ask for a verdict filter. Omitted, the tally is a readout. */
  onSelect?: (id: string) => void
  selected?: string | null
}

export interface ItemGridProps {
  cells: GridCellModel[]
  /** The bag is 12×5. A quad stash tab is 24×24 and will not fit `compact`; that
   * is a screen-level decision (`profiles: ['full']`), not a grid one. */
  cols: Hint<number>
  rows: Hint<number>
  selected?: string | null
  /**
   * The only navigation surface a module gets. `compact` wires this to
   * `Focusable`'s `onGamepadFocus` — Steam's focus system resolves D-pad direction
   * geometrically against live DOM rects, so a CSS grid navigates in 2D for free
   * (SPEC §6.1) — and `full` wires it to click plus arrow keys. A module that
   * wanted `onKeyDown` here would be a module that works on one surface.
   */
  onSelect?: (uid: string) => void
  /** `syncing` dims the grid. It never blanks it: a grid that disappears while a
   * refresh is in flight is a grid the player stops trusting to be there. */
  dimmed?: boolean
  /** Shown in place of the grid when `cells` is empty. */
  emptyLabel?: string
  /** Accessible name for the whole grid. */
  label?: string
}

export interface ItemRowProps {
  item: ItemRowModel
  selected?: boolean
  onSelect?: (uid: string) => void
  /**
   * Which optional parts to draw. `compact` gets name, quantity and price; `full`
   * adds the reason and the marks. Same row model, two amounts of it.
   */
  fields?: Hint<ItemRowField[]>
}

export type ItemRowField = 'subtitle' | 'quantity' | 'price' | 'reason' | 'marks' | 'provenance'

export interface ValueBarProps {
  bars: BarModel[]
  /** Bars are drawn against this. Defaults to the largest value present. */
  max?: number
  /** `{ compact: 3, full: null }`. Same truncate-and-report rule as `Section`. */
  limit?: Hint<number | null>
  label?: string
}

export interface VerdictPillProps {
  verdict: Verdict
  /** Defaults to the verdict's own word. */
  label?: string
  count?: number
  size?: Hint<'sm' | 'md'>
}

// ----------------------------------------------------------- interaction ----

export interface ActionProps {
  label: string
  onPress: () => void
  kind?: 'primary' | 'default' | 'quiet'
  disabled?: boolean
  /**
   * Live countdown in seconds. While it is a positive number the control is
   * disabled and its label carries the remaining time. This is a first-class prop
   * because the alternative — a module formatting `Retry-After` into `label` and
   * setting `disabled` — puts a per-second re-render and a piece of policy in the
   * feature, and gets forgotten exactly once, on the screen where a swallowed
   * press is a player pressing harder.
   */
  countdown?: number | null
  /** A spinner and a disabled control, without changing the label. */
  busy?: boolean
  /** Gamepad button at `compact` (`A`, `Y`, `X`), keyboard hint at `full`. */
  hint?: string
}

export interface CheckListProps {
  options: CheckOptionModel[]
  /** Ticked ids. Controlled: the kit owns no selection state. */
  selected: string[]
  onToggle: (id: string) => void
  /** Accessible name for the group. */
  label?: string
  /**
   * Which optional parts of a row to draw. `compact` keeps the badge — the tier is
   * the whole reason the list is worth reading — and drops `meta`.
   *
   * There is deliberately **no `limit`**. Every other list in this kit truncates and
   * reports (rule 5 in this file's docstring); a checkbox list may not, because a
   * hidden row is a filter the player cannot switch off and cannot see. Six affixes
   * is the game's own ceiling, so the list is bounded by the item rather than by us.
   */
  fields?: Hint<CheckField[]>
  emptyLabel?: string
}

export interface StepperProps {
  label: string
  /**
   * `null` means the question is **not being asked**, which is a different state
   * from asking for zero: "at least 0 open prefixes" matches every item and is a
   * filter that does nothing, while `null` leaves the filter out of the query.
   */
  value: number | null
  onChange: (value: number | null) => void
  min?: number
  max?: number
  /** Shown in place of the number when `value` is `null`. */
  offLabel?: string
  /** One line under the control — where "counts uncertain" goes. */
  note?: string
  tone?: Tone
  /** Stepping below `min` clears to `null` when this is set. */
  clearable?: boolean
}

export interface FocusProps {
  /**
   * A focus region.
   *
   * **A module never needs this** — §2.3 puts navigation in the kit, and `ItemGrid`,
   * `ItemRow`, `Action` and `Tally` already carry their own focus behaviour. It is
   * exported because a *surface shell* composing several screens does need to name
   * regions, and because `compact` needs somewhere to put a `FocusRing` that is not
   * inside a data primitive.
   */
  id: string
  label?: string
  /** Fires with the id of the focused descendant, or `null` on leaving. */
  onFocusChange?: (uid: string | null) => void
  children: Slot
}

export interface DetailProps {
  item: DetailModel | null
  /**
   * Which fields, per profile. The plan's own example:
   * `{ compact: ['name','value'], full: ['name','value','mods','comparables'] }`.
   * Order is the order they are drawn in.
   */
  fields: Hint<DetailField[]>
  /** Shown when `item` is null. `compact` needs this to be short. */
  empty?: string
  actions?: Slot
}

// ----------------------------------------------------------------- state ----

export interface PendingProps {
  label?: string
  /** How many skeleton rows to reserve, so the layout does not jump on arrival. */
  rows?: Hint<number>
}

export interface EmptyProps {
  title: string
  detail?: string
  action?: Slot
}

export interface ErrorStateProps {
  title: string
  detail?: string
  /** `kind` distinguishes "this failed" from "this is not available here". */
  kind?: 'failure' | 'unavailable'
  action?: Slot
}

export interface StaleBannerProps {
  status: SyncStatus
  /** Omitted, the banner is a readout with no control. */
  onRefresh?: () => void
  refreshLabel?: string
  /**
   * Overrides the wording for a state. Used sparingly — the point of this
   * primitive is that six states get six honest sentences by default, in one place,
   * rather than six sentences per screen.
   */
  labels?: Partial<Record<SyncStatus['state'], string>>
}

// ------------------------------------------------- component type aliases ----

/**
 * Each profile exports `const X: XComponent`, so a `compact` implementation whose
 * signature has drifted from `full` is a type error at build time rather than a
 * Phase 7 discovery. This is the mechanism behind §2.3's "designed against both".
 */
export type Component<P> = (props: P) => ReactElement | null

export type ScreenComponent = Component<ScreenProps>
export type SectionComponent = Component<SectionProps>
export type StackComponent = Component<StackProps>
export type RowComponent = Component<RowProps>
export type StatComponent = Component<StatProps>
export type TallyComponent = Component<TallyProps>
export type ItemGridComponent = Component<ItemGridProps>
export type ItemRowComponent = Component<ItemRowProps>
export type ValueBarComponent = Component<ValueBarProps>
export type VerdictPillComponent = Component<VerdictPillProps>
export type ActionComponent = Component<ActionProps>
export type CheckListComponent = Component<CheckListProps>
export type StepperComponent = Component<StepperProps>
export type FocusComponent = Component<FocusProps>
export type DetailComponent = Component<DetailProps>
export type PendingComponent = Component<PendingProps>
export type EmptyComponent = Component<EmptyProps>
export type ErrorStateComponent = Component<ErrorStateProps>
export type StaleBannerComponent = Component<StaleBannerProps>

/**
 * The whole kit, as one type. A profile module is checked against this in its own
 * `index.tsx`, which is what turns "we wrote both signatures" into something the
 * compiler agrees with.
 */
export interface KitImplementation {
  Screen: ScreenComponent
  Section: SectionComponent
  Stack: StackComponent
  Row: RowComponent
  Stat: StatComponent
  Tally: TallyComponent
  ItemGrid: ItemGridComponent
  ItemRow: ItemRowComponent
  ValueBar: ValueBarComponent
  VerdictPill: VerdictPillComponent
  Action: ActionComponent
  CheckList: CheckListComponent
  Stepper: StepperComponent
  Focus: FocusComponent
  Detail: DetailComponent
  Pending: PendingComponent
  Empty: EmptyComponent
  ErrorState: ErrorStateComponent
  StaleBanner: StaleBannerComponent
}
