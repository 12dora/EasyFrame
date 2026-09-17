import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** 顶栏通知钩子:空闲后首拉、无权限不拉、乐观忽略失败时重拉。 */

const { loadNotifications, dismissNotification, dismissAllNotifications, idleRuns } = vi.hoisted(() => ({
  loadNotifications: vi.fn(),
  dismissNotification: vi.fn(),
  dismissAllNotifications: vi.fn(),
  idleRuns: [] as Array<() => void>,
}));

vi.mock("../lib/shell-adapter", () => ({ loadNotifications, dismissNotification, dismissAllNotifications }));
vi.mock("../lib/identity-cache", () => ({ scheduleWhenIdle: (run: () => void) => { idleRuns.push(run); return () => undefined; } }));

const { useShellNotifications } = await import("./use-shell-notifications");

type Result = ReturnType<typeof useShellNotifications>;
let latest: Result;
function Probe({ enabled }: { enabled: boolean }) {
  latest = useShellNotifications(enabled, "/zh-CN/app/notifications");
  return null;
}

const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
let container: HTMLDivElement;
let root: Root;
const ITEMS = [{ id: "n1", title: "a", detail: "", urgent: false }, { id: "n2", title: "b", detail: "", urgent: true }];

beforeEach(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  idleRuns.length = 0;
  loadNotifications.mockResolvedValue(ITEMS);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

describe("useShellNotifications", () => {
  it("returns undefined and never fetches when disabled", () => {
    act(() => root.render(<Probe enabled={false} />));
    expect(latest).toBeUndefined();
    expect(idleRuns).toHaveLength(0);
    expect(loadNotifications).not.toHaveBeenCalled();
  });

  it("defers the first fetch until the browser is idle", async () => {
    act(() => root.render(<Probe enabled />));
    expect(loadNotifications).not.toHaveBeenCalled();
    expect(latest?.viewAllHref).toBe("/zh-CN/app/notifications");
    act(() => idleRuns[0]());
    await flush();
    expect(latest?.items.map((item) => item.id)).toEqual(["n1", "n2"]);
    expect(latest?.loading).toBe(false);
  });

  it("dismisses optimistically and refetches when the dismiss fails", async () => {
    act(() => root.render(<Probe enabled />));
    act(() => idleRuns[0]());
    await flush();
    dismissNotification.mockRejectedValue(new Error("boom"));
    loadNotifications.mockResolvedValue([ITEMS[1]]);
    await act(async () => { await latest?.onDismiss?.("n1"); });
    await flush();
    expect(loadNotifications).toHaveBeenCalledTimes(2);
    expect(latest?.items.map((item) => item.id)).toEqual(["n2"]);
  });

  it("marks the error state when loading fails", async () => {
    loadNotifications.mockRejectedValue(new Error("down"));
    act(() => root.render(<Probe enabled />));
    act(() => idleRuns[0]());
    await flush();
    expect(latest?.error).toBe(true);
  });
});
