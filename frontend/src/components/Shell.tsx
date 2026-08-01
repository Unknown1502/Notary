import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Button,
  IconChevron,
  IconEvidence,
  IconLibrary,
  IconMoon,
  IconPanel,
  IconQueue,
  IconReview,
  IconSearch,
  IconSun,
  Kbd,
  isMac,
  useHotkey,
} from "./ui";
import type { Health } from "../types";

export type Tab = "review" | "queue" | "library" | "evidence";

const NAV: { id: Tab; label: string; Icon: (p: { className?: string }) => JSX.Element }[] =
  [
    { id: "review", label: "Review", Icon: IconReview },
    { id: "queue", label: "Human queue", Icon: IconQueue },
    { id: "library", label: "Certified", Icon: IconLibrary },
    { id: "evidence", label: "Evidence", Icon: IconEvidence },
  ];

/* ==========================================================================
   Command palette

   Present because this is a console for someone who lives in it all day, not
   a marketing page. It navigates and it runs the two actions a reviewer takes
   constantly — nothing else. A palette stuffed with every route in the app is
   a menu with extra steps.
   ========================================================================== */

type Command = {
  id: string;
  label: string;
  hint?: string;
  group: string;
  run: () => void;
};

function CommandPalette({
  open,
  onClose,
  commands,
}: {
  open: boolean;
  onClose: () => void;
  commands: Command[];
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      // Defer so the element exists before focus.
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter((c) =>
      `${c.label} ${c.group} ${c.hint ?? ""}`.toLowerCase().includes(q),
    );
  }, [commands, query]);

  useEffect(() => setActive(0), [query]);

  if (!open) return null;

  const grouped = results.reduce<Record<string, Command[]>>((acc, c) => {
    (acc[c.group] ??= []).push(c);
    return acc;
  }, {});

  let flat = -1;

  return (
    <AnimatePresence>
      <motion.div
        className="scrim"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.12 }}
        onMouseDown={(e) => e.target === e.currentTarget && onClose()}
      >
        <motion.div
          className="cmdk"
          role="dialog"
          aria-modal="true"
          aria-label="Command palette"
          initial={{ opacity: 0, y: -8, scale: 0.985 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -4, scale: 0.99 }}
          transition={{ type: "spring", stiffness: 460, damping: 36 }}
        >
          <input
            ref={inputRef}
            className="cmdk__input"
            placeholder="Search reviews, verdicts, actions…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") return onClose();
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setActive((i) => Math.min(i + 1, results.length - 1));
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                setActive((i) => Math.max(i - 1, 0));
              }
              if (e.key === "Enter" && results[active]) {
                e.preventDefault();
                results[active].run();
                onClose();
              }
            }}
          />

          <div className="cmdk__list">
            {results.length === 0 && (
              <p className="cmdk__empty">No matches for “{query}”</p>
            )}
            {Object.entries(grouped).map(([group, items]) => (
              <div key={group}>
                <p className="cmdk__group label">{group}</p>
                {items.map((c) => {
                  flat += 1;
                  const index = flat;
                  return (
                    <button
                      key={c.id}
                      className="cmdk__item"
                      data-active={index === active}
                      onMouseEnter={() => setActive(index)}
                      onClick={() => {
                        c.run();
                        onClose();
                      }}
                    >
                      <span>{c.label}</span>
                      {c.hint && <span className="muted">{c.hint}</span>}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

/* ==========================================================================
   Shell
   ========================================================================== */

export function Shell({
  tab,
  onTab,
  health,
  queueDepth,
  theme,
  onToggleTheme,
  crumb,
  children,
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
  health: Health | null;
  queueDepth: number;
  theme: "dark" | "light";
  onToggleTheme: () => void;
  crumb: ReactNode;
  children: ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);

  useHotkey({ key: "k", meta: true }, () => setPaletteOpen((v) => !v));
  useHotkey({ key: "escape" }, () => setPaletteOpen(false));
  useHotkey({ key: "b", meta: true }, () => setCollapsed((v) => !v));

  const commands = useMemo<Command[]>(
    () => [
      ...NAV.map((n) => ({
        id: `go-${n.id}`,
        label: n.label,
        group: "Navigate",
        hint: n.id === "queue" && queueDepth ? `${queueDepth} waiting` : undefined,
        run: () => onTab(n.id),
      })),
      {
        id: "theme",
        label: theme === "dark" ? "Switch to light" : "Switch to dark",
        group: "Preferences",
        run: onToggleTheme,
      },
      {
        id: "sidebar",
        label: collapsed ? "Expand sidebar" : "Collapse sidebar",
        group: "Preferences",
        hint: isMac ? "⌘B" : "Ctrl B",
        run: () => setCollapsed((v) => !v),
      },
    ],
    [collapsed, onTab, onToggleTheme, queueDepth, theme],
  );

  const mode = health?.runtime.mode ?? "…";
  const sealed = health?.storage?.buckets?.vault?.object_lock === "Enabled";

  return (
    <div className="shell" data-collapsed={collapsed}>
      <aside className="sidebar" data-open={mobileOpen}>
        <div className="brand">
          <span className="brand__mark" aria-hidden="true" />
          <span className="brand__name">Notary</span>
        </div>

        <nav className="nav" aria-label="Sections">
          <p className="nav__section label">Console</p>
          {NAV.map(({ id, label, Icon }) => (
            <button
              key={id}
              className="nav__item"
              aria-current={tab === id ? "page" : undefined}
              onClick={() => {
                onTab(id);
                setMobileOpen(false);
              }}
            >
              <Icon className="nav__icon" />
              <span>{label}</span>
              {id === "queue" && queueDepth > 0 && (
                <span className="nav__count">{queueDepth}</span>
              )}
            </button>
          ))}
        </nav>

        <div className="sidebar__foot">
          <div className="readout">
            <span className="readout__k">Mode</span>
            <span className="readout__v">{mode}</span>
            <span className="readout__k">Trust</span>
            <span className="readout__v">
              mode {health?.runtime.trust_mode ?? "–"}
            </span>
            <span className="readout__k">Vault</span>
            <span className="readout__v">{sealed ? "locked" : "open"}</span>
            <span className="readout__k">Retention</span>
            <span className="readout__v">{health?.runtime.retention_days ?? "–"}d</span>
          </div>
        </div>
      </aside>

      <div>
        <header className="topbar">
          <Button
            icon
            variant="ghost"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={() => {
              setCollapsed((v) => !v);
              setMobileOpen((v) => !v);
            }}
          >
            <IconPanel />
          </Button>

          <div className="crumb">
            <span className="muted">Notary</span>
            <IconChevron className="crumb__sep" />
            {crumb}
          </div>

          <div className="topbar__right">
            <Button variant="ghost" onClick={() => setPaletteOpen(true)}>
              <IconSearch />
              <span className="muted">Search</span>
              <Kbd>{isMac ? "⌘" : "Ctrl"}</Kbd>
              <Kbd>K</Kbd>
            </Button>
            <Button
              icon
              variant="ghost"
              aria-label={theme === "dark" ? "Use light theme" : "Use dark theme"}
              onClick={onToggleTheme}
            >
              {theme === "dark" ? <IconSun /> : <IconMoon />}
            </Button>
          </div>
        </header>

        <main className="main">{children}</main>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        commands={commands}
      />
    </div>
  );
}
