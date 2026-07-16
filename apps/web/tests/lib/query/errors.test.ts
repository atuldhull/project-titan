import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

import { toast } from "sonner";

import { ApiError } from "@titan/api-client";

import { handleMutationError } from "@/lib/query/errors";

/**
 * TITAN-FRT-037: the server is the RBAC authority. When a forged mutation from
 * an under-privileged session comes back 403, the UI must surface a permission
 * toast rather than fail silently.
 */
describe("handleMutationError", () => {
  beforeEach(() => {
    vi.mocked(toast.error).mockClear();
  });

  it("shows a permission toast on a 403", () => {
    handleMutationError(
      new ApiError(403, {
        type: "about:blank",
        title: "Forbidden",
        status: 403,
      }),
    );
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(vi.mocked(toast.error).mock.calls[0][0]).toMatch(/permission/i);
  });

  it("prompts a re-login on a 401", () => {
    handleMutationError(
      new ApiError(401, {
        type: "about:blank",
        title: "Unauthorized",
        status: 401,
      }),
    );
    expect(vi.mocked(toast.error).mock.calls[0][0]).toMatch(/sign in/i);
  });

  it("stays quiet for other API errors and non-API errors", () => {
    handleMutationError(
      new ApiError(500, { type: "about:blank", title: "Boom", status: 500 }),
    );
    handleMutationError(new Error("network"));
    expect(toast.error).not.toHaveBeenCalled();
  });
});
