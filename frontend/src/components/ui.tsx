import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { AnimatePresence, motion } from "framer-motion";

/* ==========================================================================
   Icons

   Hand-drawn at 16px on a 16px grid, 1.5 stroke, currentColor. There are
   exactly as many as the interface needs and no more — an icon set is a
   vocabulary, and an oversized vocabulary is how interfaces start decorating
   instead of communicating.
   ========================================================================== */

type IconProps = { className?: string };

const svg = (children: ReactNode) =>
  function Icon({ className }: IconProps) {
    return (
      <svg
        className={className}
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        {children}
      </svg>
    );
  };

/** Review: a frame under inspection. */
export const IconReview = svg(
  <>
    <rect x="1.75" y="3.25" width="12.5" height="9.5" rx="1.5" />
    <path d="M6.25 6.5 10 8l-3.75 1.5z" fill="currentColor" stroke="none" />
  </>,
);

/** Queue: an inbox with one item resting in it. */
export const IconQueue = svg(
  <>
    <path d="M1.75 9.25h3l1 1.75h4.5l1-1.75h3" />
    <path d="M3.4 3.25h9.2l1.65 6v3a1.5 1.5 0 0 1-1.5 1.5H3.25a1.5 1.5 0 0 1-1.5-1.5v-3z" />
  </>,
);

/** Library: sealed records stacked. */
export const IconLibrary = svg(
  <>
    <rect x="1.75" y="2.75" width="12.5" height="3" rx="1" />
    <path d="M2.75 5.75v6a1.5 1.5 0 0 0 1.5 1.5h7.5a1.5 1.5 0 0 0 1.5-1.5v-6" />
    <path d="M6.5 8.75h3" />
  </>,
);

/** Evidence: a proof mark inside a bound field. */
export const IconEvidence = svg(
  <>
    <path d="M8 1.75 13.25 4v4.1c0 3-2.2 5.1-5.25 6.15C4.95 13.2 2.75 11.1 2.75 8.1V4z" />
    <path d="M5.9 8.1 7.4 9.6l2.9-3" />
  </>,
);

export const IconSearch = svg(
  <>
    <circle cx="7.25" cy="7.25" r="4.5" />
    <path d="m10.6 10.6 2.65 2.65" />
  </>,
);

export const IconChevron = svg(<path d="m6 3.5 4.5 4.5L6 12.5" />);

export const IconCheck = svg(<path d="m3.25 8.5 3 3 6.5-7" />);

export const IconClose = svg(
  <>
    <path d="m4 4 8 8" />
    <path d="m12 4-8 8" />
  </>,
);

export const IconPanel = svg(
  <>
    <rect x="1.75" y="2.75" width="12.5" height="10.5" rx="1.5" />
    <path d="M6.25 2.75v10.5" />
  </>,
);

export const IconSun = svg(
  <>
    <circle cx="8" cy="8" r="3" />
    <path d="M8 1.5v1.2M8 13.3v1.2M14.5 8h-1.2M2.7 8H1.5M12.6 3.4l-.85.85M4.25 11.75l-.85.85M12.6 12.6l-.85-.85M4.25 4.25l-.85-.85" />
  </>,
);

export const IconMoon = svg(
  <path d="M13.5 9.4A5.75 5.75 0 0 1 6.6 2.5a5.75 5.75 0 1 0 6.9 6.9" />,
);

export const IconShield = svg(
  <path d="M8 1.75 13.25 4v4.1c0 3-2.2 5.1-5.25 6.15C4.95 13.2 2.75 11.1 2.75 8.1V4z" />,
);

/* ==========================================================================
   Primitives
   ========================================================================== */

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "accent" | "outline" | "ghost" | "danger";
  size?: "md" | "sm";
  icon?: boolean;
};

export function Button({
  variant = "outline",
  size = "md",
  icon = false,
  className = "",
  ...rest
}: ButtonProps) {
  return (
    <button
      className={[
        "btn",
        `btn--${variant}`,
        size === "sm" ? "btn--sm" : "",
        icon ? "btn--icon" : "",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
      {...rest}
    />
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

export function Pill({
  children,
  accent = false,
  title,
}: {
  children: ReactNode;
  accent?: boolean;
  title?: string;
}) {
  return (
    <span className={`pill${accent ? " pill--accent" : ""}`} title={title}>
      {children}
    </span>
  );
}

export function Panel({
  title,
  actions,
  children,
  flush = false,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="panel">
      {title !== undefined && (
        <header className="panel__head">
          <h2 className="panel__title">{title}</h2>
          {actions && <div className="panel__actions">{actions}</div>}
        </header>
      )}
      <div className={`panel__body${flush ? " panel__body--flush" : ""}`}>
        {children}
      </div>
    </section>
  );
}

export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <p className="empty__title">{title}</p>
      <p className="empty__body">{children}</p>
      {action}
    </div>
  );
}

export function Notice({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "accent" | "fail";
  children: ReactNode;
}) {
  return (
    <div
      className={`notice${tone === "neutral" ? "" : ` notice--${tone}`}`}
      role={tone === "fail" ? "alert" : undefined}
    >
      <span>{children}</span>
    </div>
  );
}

export function Skeleton({
  height = 12,
  width = "100%",
}: {
  height?: number;
  width?: number | string;
}) {
  return <span className="skeleton" style={{ height, width, display: "block" }} />;
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="stat__v">{value}</div>
    </div>
  );
}

/** A threshold bar. `ratio` is 0–1 of the way to the limit. */
export function Gauge({ ratio }: { ratio: number }) {
  const clamped = Math.max(0.04, Math.min(1, ratio));
  return (
    <span className="gauge" aria-hidden="true">
      <motion.span
        className="gauge__fill"
        initial={{ scaleX: 0 }}
        animate={{ scaleX: clamped }}
        transition={{ type: "spring", stiffness: 260, damping: 30 }}
        style={{ width: "100%" }}
      />
    </span>
  );
}

/* ==========================================================================
   Toasts

   Deliberately minimal: a rule, a line of text, auto-dismiss. A toast is an
   acknowledgement, not a dialog — anything that needs a decision belongs
   inline where the decision is made.
   ========================================================================== */

type Tone = "neutral" | "success" | "error" | "accent";
type Toast = { id: number; message: ReactNode; tone: Tone };

const ToastContext = createContext<(message: ReactNode, tone?: Tone) => void>(
  () => undefined,
);

export const useToast = () => useContext(ToastContext);

export function ToastHost({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);

  const push = useCallback((message: ReactNode, tone: Tone = "neutral") => {
    const id = ++seq.current;
    setToasts((t) => [...t, { id, message, tone }]);
    window.setTimeout(
      () => setToasts((t) => t.filter((x) => x.id !== id)),
      tone === "error" ? 6500 : 4000,
    );
  }, []);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        <AnimatePresence initial={false}>
          {toasts.map((t) => (
            <motion.div
              key={t.id}
              className="toast"
              data-tone={t.tone}
              initial={{ opacity: 0, y: 8, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 4, scale: 0.98 }}
              transition={{ type: "spring", stiffness: 420, damping: 34 }}
            >
              <span className="toast__bar" />
              <span>{t.message}</span>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  );
}

/* ==========================================================================
   Theme
   ========================================================================== */

export function useTheme() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    const stored = localStorage.getItem("notary-theme");
    if (stored === "light" || stored === "dark") return stored;
    return window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("notary-theme", theme);
  }, [theme]);

  return useMemo(
    () => ({ theme, toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")) }),
    [theme],
  );
}

/* ==========================================================================
   Keyboard
   ========================================================================== */

export function useHotkey(
  combo: { key: string; meta?: boolean },
  handler: () => void,
) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);

      const metaHeld = e.metaKey || e.ctrlKey;
      if (combo.meta && !metaHeld) return;
      if (!combo.meta && (metaHeld || typing)) return;
      if (e.key.toLowerCase() !== combo.key) return;

      e.preventDefault();
      handler();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [combo.key, combo.meta, handler]);
}

/** True on Apple platforms, so the palette hint shows ⌘ rather than Ctrl. */
export const isMac =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
