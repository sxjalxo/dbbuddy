import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ElementType,
  type ReactNode,
} from "react";
import { cn } from "@/lib/utils";

type Variant = "up" | "down" | "left" | "right" | "scale";

/**
 * Full-motion hidden state per variant. The element interpolates from this
 * transform + opacity-0 to its resting state, so it glides into place.
 *
 * The premium feel (Linear / Vercel / Stripe) combines three axes at once:
 * opacity 0 → 1, a small translate (~32px), and a subtle scale 0.98 → 1.
 *
 * Under `prefers-reduced-motion: reduce`, the scoped `.reveal` rule in styles.css
 * forces `translate`/`scale` to `none` and shortens the duration — so the same
 * markup degrades to a quick, movement-free opacity fade instead of snapping.
 */
const hiddenClass: Record<Variant, string> = {
  up: "opacity-0 translate-y-8 scale-[0.98]",
  down: "opacity-0 -translate-y-8 scale-[0.98]",
  left: "opacity-0 translate-x-8 scale-[0.98]",
  right: "opacity-0 -translate-x-8 scale-[0.98]",
  scale: "opacity-0 scale-[0.98]",
};

const shownClass = "opacity-100 translate-x-0 translate-y-0 scale-100";

const transitionClass =
  "transition-all duration-700 ease-[cubic-bezier(0.16,1,0.3,1)] will-change-[opacity,transform]";

// useLayoutEffect runs before paint (so we can hide below-fold content without a
// flash), but warns during SSR — fall back to useEffect on the server.
const useIsomorphicLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

interface RevealProps {
  children: ReactNode;
  /** Element to render. Defaults to a div. */
  as?: ElementType;
  /** Direction the element drifts in from. */
  variant?: Variant;
  /** Stagger delay in milliseconds. */
  delay?: number;
  className?: string;
  style?: CSSProperties;
  /** IntersectionObserver threshold (0–1). */
  amount?: number;
  /**
   * Render visible immediately and skip the scroll observer. Use for content
   * that is intentionally above the fold and should never gate on JS.
   */
  eager?: boolean;
}

/**
 * Reveals content as it scrolls into view — fail-open and flash-free.
 *
 * Unlike a naive "start hidden, reveal on intersect" reveal (which ships every
 * section at opacity-0 in the SSR HTML and leaves the page blank until hydration),
 * this renders **visible by default** and only *hides* an element on the client,
 * before paint, when it genuinely starts below the fold. Consequences:
 *
 *  - SSR / no-JS / slow-JS never show a blank section (content is visible by default).
 *  - Above-the-fold content appears immediately with no entrance animation.
 *  - Only genuinely-below-fold content fades + lifts in on scroll, once.
 *  - It never replays when scrolling back up (the observer disconnects).
 *
 * Reduced-motion is handled gracefully in styles.css (movement stripped, a short
 * opacity fade kept), not by disabling the reveal here.
 */
export function Reveal({
  children,
  as: Tag = "div",
  variant = "up",
  delay = 0,
  className,
  style,
  amount = 0.18,
  eager = false,
}: RevealProps) {
  const ref = useRef<HTMLElement | null>(null);
  // null = undecided (render visible, no transition — matches SSR and fails open).
  // false = hidden (armed: will transition in on scroll). true = shown.
  const [revealed, setRevealed] = useState<boolean | null>(eager ? true : null);
  // Latches once an element has been hidden, so only those get the transition +
  // stagger delay. Elements shown immediately render in final state instantly.
  const wasHidden = useRef(false);

  // Decide visibility and arm the observer exactly once, on mount. We must not
  // depend on `revealed` here: re-running on every state change would tear down
  // and re-arm the observer (and breaks under React Strict Mode's double-invoke).
  useIsomorphicLayoutEffect(() => {
    const el = ref.current;
    if (eager || !el) return;

    // Fail open: without an observer, just show.
    if (typeof IntersectionObserver === "undefined") {
      setRevealed(true);
      return;
    }

    const viewportH = window.innerHeight || document.documentElement.clientHeight;
    // Only hide content that starts entirely below the fold — anything already
    // (even partly) visible shows immediately, so there is no hide-then-show flash.
    if (el.getBoundingClientRect().top < viewportH) {
      setRevealed(true);
      return;
    }

    wasHidden.current = true;
    setRevealed(false);

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) {
          setRevealed(true);
          observer.disconnect();
        }
      },
      { threshold: amount, rootMargin: "0px 0px -10% 0px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
    // Mount-once on purpose: `amount` and `eager` are fixed per instance, and
    // re-running would tear down and re-arm the observer.
  }, []);

  const isHidden = revealed === false;
  const animates = wasHidden.current; // true once it has been hidden at least once
  const Component = Tag as ElementType;

  return (
    <Component
      ref={ref}
      className={cn(
        "reveal",
        animates && transitionClass,
        isHidden ? hiddenClass[variant] : shownClass,
        className,
      )}
      style={animates && !isHidden && delay ? { ...style, transitionDelay: `${delay}ms` } : style}
    >
      {children}
    </Component>
  );
}
