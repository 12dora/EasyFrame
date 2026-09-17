import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** 顶栏通知钩子:空闲后首拉、无权限不拉、乐观忽略失败时重拉、换人 / 关权限清空。 */

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
function Probe({ enabled, account = "u1" }: { enabled: boolean; account?: string }) {
  latest = useShellNotifications(enabled, "/zh-CN/app/notifications", account);
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

  it("dismisses all optimistically and refetches when it fails", async () => {
    act(() => root.render(<Probe enabled />));
    act(() => idleRuns[0]());
    await flush();
    dismissAllNotifications.mockRejectedValue(new Error("boom"));
    await act(async () => { await latest?.onDismissAll?.(); });
    await flush();
    expect(loadNotifications).toHaveBeenCalledTimes(2);
    expect(latest?.items.map((item) => item.id)).toEqual(["n1", "n2"]);
  });

  it("refetches when the bell is opened", async () => {
    act(() => root.render(<Probe enabled />));
    act(() => latest?.onOpen?.());
    await flush();
    expect(loadNotifications).toHaveBeenCalledTimes(1);
    expect(latest?.items).toHaveLength(2);
  });

  // 同标签页对账换了人:铃铛里不能留上一个人的通知。
  it("clears and refetches when the account changes in place", async () => {
    act(() => root.render(<Probe enabled account="u1" />));
    act(() => idleRuns[0]());
    await flush();
    expect(latest?.items).toHaveLength(2);
    loadNotifications.mockResolvedValue([{ id: "b1", title: "李四的", detail: "", urgent: false }]);
    act(() => root.render(<Probe enabled account="u2" />));
    expect(latest?.items).toEqual([]);
    expect(idleRuns).toHaveLength(2);
    act(() => idleRuns[1]());
    await flush();
    expect(loadNotifications).toHaveBeenCalledTimes(2);
    expect(latest?.items.map((item) => item.id)).toEqual(["b1"]);
  });

  it("drops a response that was in flight for the previous account", async () => {
    let settleOld: (items: unknown[]) => void = () => undefined;
    loadNotifications.mockReturnValueOnce(new Promise((resolve) => { settleOld = resolve; }));
    act(() => root.render(<Probe enabled account="u1" />));
    act(() => idleRuns[0]());
    act(() => root.render(<Probe enabled account="u2" />));
    await act(async () => { settleOld(ITEMS); await Promise.resolve(); });
    expect(latest?.items).toEqual([]);
    expect(latest?.loading).toBe(false);
  });

  it("stops fetching and returns undefined when disabled, then refetches from empty on re-enable", async () => {
    act(() => root.render(<Probe enabled />));
    act(() => idleRuns[0]());
    await flush();
    act(() => root.render(<Probe enabled={false} />));
    expect(latest).toBeUndefined();
    expect(idleRuns).toHaveLength(1);
    act(() => root.render(<Probe enabled />));
    expect(latest?.items).toEqual([]);
    expect(idleRuns).toHaveLength(2);
    act(() => idleRuns[1]());
    await flush();
    expect(loadNotifications).toHaveBeenCalledTimes(2);
  });

  // 旧账号的响应在「换人那一次渲染」的同一个 act 里落地(渲染与 effect 之间的微任务),也不能进新账号的铃铛。
  it("keeps a previous account's response out even when it resolves inside the switching act", async () => {
    let settleOld: (items: unknown[]) => void = () => undefined;
    loadNotifications.mockReturnValueOnce(new Promise((resolve) => { settleOld = resolve; }));
    act(() => root.render(<Probe enabled account="u1" />));
    act(() => idleRuns[0]());
    await act(async () => {
      root.render(<Probe enabled account="u2" />);
      settleOld(ITEMS);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(latest?.items).toEqual([]);
    expect(latest?.loading).toBe(false);
  });

  // `platformRequest` 按「路径 + token」合并在途 GET:token 未轮换时新账号的请求会领到旧账号那份响应。
  it("waits for the previous account's in-flight request before fetching, so it cannot join it", async () => {
    let shared: Promise<unknown[]> | null = null;
    let settleShared: (items: unknown[]) => void = () => undefined;
    let responses = 0;
    loadNotifications.mockImplementation(() => {
      if (!shared) {
        responses += 1;
        const answer = responses === 1 ? ITEMS : [{ id: "b1", title: "李四的", detail: "", urgent: false }];
        shared = new Promise((resolve) => { settleShared = () => resolve(answer); }).finally(() => { shared = null; }) as Promise<unknown[]>;
      }
      return shared;
    });
    act(() => root.render(<Probe enabled account="u1" />));
    act(() => idleRuns[0]());
    act(() => root.render(<Probe enabled account="u2" />));
    act(() => idleRuns[1]());
    await flush();
    // 旧请求还在途:新账号这一次还没发出,自然也合并不上。
    expect(loadNotifications).toHaveBeenCalledTimes(1);
    await act(async () => { settleShared([]); for (let i = 0; i < 6; i += 1) await Promise.resolve(); });
    expect(loadNotifications).toHaveBeenCalledTimes(2);
    expect(latest?.items).toEqual([]);
    await act(async () => { settleShared([]); for (let i = 0; i < 6; i += 1) await Promise.resolve(); });
    expect(latest?.items.map((item) => item.id)).toEqual(["b1"]);
  });
});
