import { useCallback, useEffect, useRef, useState } from "react";

/** Fetch now, then every `ms` while the tab is visible. `reload()` refreshes immediately. */
export function usePoll<T>(fetcher: () => Promise<T>, ms: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;

  const reload = useCallback(async () => {
    try {
      setData(await fetchRef.current());
      setError(null);
    } catch (e) {
      setError(e as Error);
    }
  }, []);

  useEffect(() => {
    setData(null);
    reload();
    const timer = setInterval(() => document.visibilityState === "visible" && reload(), ms);
    const onVisible = () => document.visibilityState === "visible" && reload();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, reload, ...deps]);

  return { data, error, reload, setData };
}
