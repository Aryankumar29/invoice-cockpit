import { useEffect, useRef, useState } from "react";
import { api } from ".";
import type { RunDetail, StageEvent } from "./types";

export type Transport = "connecting" | "live" | "polling" | "closed";

const POLL_MS = 1000;

/** `?transport=poll` (sticky for the tab) forces the polling fallback, for testing a dead SSE path. */
export function pollingForced(): boolean {
  try {
    const q = new URLSearchParams(window.location.search).get("transport");
    if (q) sessionStorage.setItem("cockpit-transport", q);
    return (q ?? sessionStorage.getItem("cockpit-transport")) === "poll";
  } catch {
    return false;
  }
}

/**
 * Live stage events for one run: seeds from the RunDetail snapshot, then follows SSE from
 * the last seq. If SSE errors, falls back to polling events.json?after=seq every second.
 * Events are de-duplicated by seq. `onDone` fires once when the run finishes.
 */
export function useRunStream(id: string, snapshot: RunDetail | undefined, onDone: () => void) {
  const [events, setEvents] = useState<StageEvent[]>([]);
  const [transport, setTransport] = useState<Transport>("connecting");
  const lastSeq = useRef(0);
  const seen = useRef(new Set<number>());
  const started = useRef<string | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  const merge = (incoming: StageEvent[]) => {
    const fresh = incoming.filter((e) => !seen.current.has(e.seq));
    if (!fresh.length) return;
    fresh.forEach((e) => seen.current.add(e.seq));
    lastSeq.current = Math.max(lastSeq.current, ...fresh.map((e) => e.seq));
    setEvents((prev) => [...prev, ...fresh].sort((a, b) => a.seq - b.seq));
  };

  const snapRef = useRef(snapshot);
  snapRef.current = snapshot;
  const ready = !!snapshot;

  // Later snapshots (refetches) may carry events the stream missed; merging is idempotent.
  useEffect(() => {
    if (snapshot && started.current === id) merge(snapshot.stages ?? []);
  }, [snapshot, id]);

  useEffect(() => {
    const snapshot = snapRef.current;
    if (!snapshot || started.current === id) return;
    started.current = id;
    lastSeq.current = 0;
    seen.current = new Set();
    setEvents([]);
    merge(snapshot.stages ?? []);
    if (snapshot.status === "done" || snapshot.status === "failed") {
      setTransport("closed");
      return;
    }

    let cancelled = false;
    let closeStream: (() => void) | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const finish = () => {
      if (cancelled) return;
      cancelled = true;
      setTransport("closed");
      doneRef.current();
    };

    const poll = async () => {
      if (cancelled) return;
      try {
        const [evs, run] = await Promise.all([api.events(id, lastSeq.current), api.run(id)]);
        merge(evs);
        if (run.status === "done" || run.status === "failed") {
          merge(run.stages ?? []);
          return finish();
        }
      } catch { /* keep polling; the chip shows we are not live */ }
      timer = setTimeout(poll, POLL_MS);
    };
    const startPolling = () => {
      if (cancelled) return;
      setTransport("polling");
      poll();
    };

    const forcePoll = pollingForced();
    if (forcePoll || typeof EventSource === "undefined") {
      startPolling();
    } else {
      setTransport("connecting");
      closeStream = api.stream(id, lastSeq.current, {
        onEvent: (e) => { setTransport("live"); merge([e]); },
        onDone: () => {
          // Pick up anything the stream did not carry before declaring the run finished.
          api.events(id, lastSeq.current).then(merge).catch(() => undefined).finally(finish);
        },
        onError: startPolling,
      });
    }

    return () => {
      cancelled = true;
      closeStream?.();
      if (timer) clearTimeout(timer);
      started.current = null;
    };
  }, [id, ready]);

  return { events, transport };
}
