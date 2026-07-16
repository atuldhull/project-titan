/**
 * requestAnimationFrame batching for high-frequency topics (TITAN-FRT-021).
 *
 * Log lines, token streams and metric samples can arrive hundreds/second; each
 * one triggering a React render would blow the interaction budget. A batcher
 * coalesces items into a single flush per animation frame. The scheduler is
 * injectable so tests can drive frames deterministically instead of leaning on
 * a real rAF.
 */
export interface FrameScheduler {
  request(callback: () => void): number;
  cancel(handle: number): void;
}

const defaultScheduler: FrameScheduler = {
  request: (cb) =>
    typeof requestAnimationFrame === "function"
      ? requestAnimationFrame(cb)
      : (setTimeout(cb, 16) as unknown as number),
  cancel: (handle) => {
    if (typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(handle);
    } else {
      clearTimeout(handle);
    }
  },
};

export interface RafBatcher<T> {
  push(item: T): void;
  /** Flush synchronously (e.g. on unmount) so nothing is dropped. */
  flush(): void;
  dispose(): void;
}

export function createRafBatcher<T>(
  onFlush: (items: T[]) => void,
  scheduler: FrameScheduler = defaultScheduler,
): RafBatcher<T> {
  let queue: T[] = [];
  let handle: number | null = null;

  const flush = (): void => {
    if (handle !== null) {
      scheduler.cancel(handle);
      handle = null;
    }
    if (queue.length === 0) {
      return;
    }
    const batch = queue;
    queue = [];
    onFlush(batch);
  };

  return {
    push(item: T): void {
      queue.push(item);
      if (handle === null) {
        handle = scheduler.request(flush);
      }
    },
    flush,
    dispose(): void {
      if (handle !== null) {
        scheduler.cancel(handle);
        handle = null;
      }
      queue = [];
    },
  };
}
