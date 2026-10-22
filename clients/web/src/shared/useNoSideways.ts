import { useEffect, type RefObject } from 'react'

/** The descendant that reaches furthest past `el`'s right edge, its box or its own text, as "tag.class"; '' when none does. */
function widestPast(el: HTMLElement): string {
  const right = el.getBoundingClientRect().right
  let worst: Element | null = null
  let most = 0
  el.querySelectorAll('*').forEach((node) => {
    const box = node.getBoundingClientRect()
    const over = Math.max(box.right, box.left + node.scrollWidth) - right
    // ties go to the later node, the deeper one: an ancestor's scrollWidth carries its child's
    if (over > 0 && over >= most) {
      most = over
      worst = node
    }
  })
  if (!worst) return ''
  const node = worst as Element
  return `${node.tagName.toLowerCase()}${node.className && typeof node.className === 'string' ? `.${node.className.trim().split(/\s+/).join('.')}` : ''}`
}

/** Whether `el` scrolls sideways: DESIGN.md §7's test, scrollWidth past clientWidth. */
export function scrollsSideways(el: HTMLElement): boolean {
  return el.scrollWidth > el.clientWidth + 1
}

/**
 * In a dev build, reports as a console error whenever the element `ref` (or the
 * nearest ancestor matching `closest`) scrolls sideways, naming the element that
 * pushes it. Unit tests lay nothing out, so this is the check that sees it.
 */
export function useNoSideways(ref: RefObject<HTMLElement | null>, name: string, closest?: string) {
  useEffect(() => {
    if (!import.meta.env.DEV || typeof ResizeObserver === 'undefined') return
    const el = closest ? ref.current?.closest<HTMLElement>(closest) : ref.current
    if (!el) return
    let reported = false
    const check = () => {
      const over = scrollsSideways(el)
      if (over && !reported) {
        console.error(
          `${name} scrolls sideways (scrollWidth ${el.scrollWidth} > clientWidth ${el.clientWidth}); widest: ${widestPast(el) || 'unknown'}. DESIGN.md §7, "Nothing sideways".`,
        )
      }
      reported = over
    }
    const sizes = new ResizeObserver(check)
    const watch = () => {
      sizes.disconnect()
      sizes.observe(el)
      Array.from(el.children).forEach((child) => sizes.observe(child))
    }
    // a fold opening or a row arriving changes a child, not the panel's own box
    const changes = new MutationObserver(() => {
      watch()
      check()
    })
    changes.observe(el, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['open', 'class'] })
    watch()
    check()
    return () => {
      sizes.disconnect()
      changes.disconnect()
    }
  }, [ref, name, closest])
}
