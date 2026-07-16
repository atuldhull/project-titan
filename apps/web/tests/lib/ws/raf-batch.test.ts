import { describe, expect, it } from "vitest";

import { createRafBatcher, type FrameScheduler } from "@/lib/ws/raf-batch";

function controllableScheduler() {
  let pending: (() => void) | null = null;
  const scheduler: FrameScheduler = {
    request: (cb) => {
      pending = cb;
      return 1;
    },
    cancel: () => {
      pending = null;
    },
  };
  return { scheduler, frame: () => pending?.() };
}

describe("createRafBatcher (TITAN-FRT-021)", () => {
  it("coalesces many pushes into a single flush per frame", () => {
    const { scheduler, frame } = controllableScheduler();
    const flushed: number[][] = [];
    const batcher = createRafBatcher<number>(
      (items) => flushed.push(items),
      scheduler,
    );

    batcher.push(1);
    batcher.push(2);
    batcher.push(3);
    expect(flushed).toHaveLength(0); // nothing until the frame fires

    frame();
    expect(flushed).toEqual([[1, 2, 3]]);
  });

  it("flushes synchronously on demand", () => {
    const { scheduler } = controllableScheduler();
    const flushed: string[][] = [];
    const batcher = createRafBatcher<string>(
      (items) => flushed.push(items),
      scheduler,
    );
    batcher.push("a");
    batcher.flush();
    expect(flushed).toEqual([["a"]]);
  });

  it("drops nothing after dispose (queue cleared, no flush)", () => {
    const { scheduler, frame } = controllableScheduler();
    const flushed: number[][] = [];
    const batcher = createRafBatcher<number>(
      (items) => flushed.push(items),
      scheduler,
    );
    batcher.push(1);
    batcher.dispose();
    frame();
    expect(flushed).toHaveLength(0);
  });
});
