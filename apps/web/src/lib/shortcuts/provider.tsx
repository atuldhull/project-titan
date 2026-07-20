"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { NO_SEQUENCE, type SequenceState } from "./keys";
import { resolve, validate, type Shortcut } from "./registry";

/**
 * The single document-level keydown listener (Vol 4 §10).
 *
 * Before this, three screens each attached their own `window` listener with
 * subtly different guards. One listener with one resolver means the ordering
 * between a screen binding and a global one is defined rather than accidental,
 * and it means the `?` overlay can enumerate what is ACTUALLY live right now
 * instead of a hand-maintained copy of it (TITAN-FRT-048).
 *
 * Screens register through `useShortcuts`, which stores a GETTER rather than the
 * array. Registering the array would re-subscribe on every render for any screen
 * that builds its handlers inline — which is most of them — and re-running an
 * effect on every render is how listener churn bugs start.
 */

type Source = () => readonly Shortcut[];

interface ShortcutContextValue {
  readonly register: (source: Source) => () => void;
  /** Every currently-registered shortcut, for the `?` overlay. */
  readonly all: readonly Shortcut[];
  readonly mac: boolean;
}

const ShortcutContext = createContext<ShortcutContextValue | null>(null);

export function ShortcutProvider({ children }: { children: React.ReactNode }) {
  const sources = useRef(new Set<Source>());
  const sequence = useRef<SequenceState>(NO_SEQUENCE);
  const [version, setVersion] = useState(0);
  const [mac, setMac] = useState(false);

  useEffect(() => {
    // Client-only: reading the platform during render would differ from the
    // server's guess and blow up hydration on the very first paint.
    setMac(/Mac|iPhone|iPad/.test(navigator.userAgent));
  }, []);

  const register = useCallback((source: Source) => {
    sources.current.add(source);
    setVersion((current) => current + 1);
    return () => {
      sources.current.delete(source);
      setVersion((current) => current + 1);
    };
  }, []);

  const collect = useCallback((): Shortcut[] => {
    const seen = new Set<string>();
    const collected: Shortcut[] = [];
    // Later registrations (a screen) shadow earlier ones (the shell) on id, so a
    // screen can specialise `n` or `/` without the global losing its overlay row.
    for (const source of sources.current) {
      for (const shortcut of source()) {
        if (seen.has(shortcut.id)) {
          const index = collected.findIndex(
            (existing) => existing.id === shortcut.id,
          );
          collected[index] = shortcut;
          continue;
        }
        seen.add(shortcut.id);
        collected.push(shortcut);
      }
    }
    return collected;
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const outcome = resolve(event, collect(), sequence.current, Date.now());
      sequence.current = outcome.sequence;
      if (outcome.kind === "prefix") {
        // Swallow the prefix so `g` does not also reach a page-level handler,
        // but do NOT preventDefault a chord or an unmatched key: that is how
        // browser defaults get eaten.
        event.preventDefault();
        return;
      }
      if (outcome.kind === "run") {
        event.preventDefault();
        outcome.shortcut.run(event);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [collect]);

  const all = useMemo(() => {
    void version; // re-collect whenever registrations change
    return collect();
  }, [collect, version]);

  const value = useMemo(() => ({ register, all, mac }), [register, all, mac]);
  return (
    <ShortcutContext.Provider value={value}>
      {children}
    </ShortcutContext.Provider>
  );
}

function useShortcutContext(): ShortcutContextValue {
  const context = useContext(ShortcutContext);
  if (!context) {
    throw new Error("useShortcuts must be used inside <ShortcutProvider>");
  }
  return context;
}

/**
 * Register shortcuts for as long as the calling component is mounted.
 *
 * The array may be rebuilt on every render — only its CONTENTS are read, at
 * event time. Handlers therefore always close over current props without the
 * caller having to memoise anything.
 */
export function useShortcuts(shortcuts: readonly Shortcut[]): void {
  const { register } = useShortcutContext();
  const latest = useRef(shortcuts);
  latest.current = shortcuts;

  if (process.env.NODE_ENV !== "production") {
    for (const shortcut of shortcuts) {
      const problem = validate(shortcut);
      if (problem) {
        throw new Error(`Invalid shortcut — ${problem}`);
      }
    }
  }

  useEffect(() => register(() => latest.current), [register]);
}

/** Every live binding, for the `?` overlay. */
export function useShortcutTable(): {
  all: readonly Shortcut[];
  mac: boolean;
} {
  const { all, mac } = useShortcutContext();
  return { all, mac };
}
