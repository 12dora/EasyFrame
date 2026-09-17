import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { hasAsyncData } from "./async-data-cache";
import { clearAsyncDataCache, invalidateAsyncData, replaceAsyncData, useAsyncData, type AsyncData } from "./use-async-data";

/**
 * 只读数据拉取的 stale-while-revalidate(契约 C1)。
 *
 * 回到页面先画上一次的结果、换筛选不闪回加载态、慢的旧响应不覆盖新键、登出清缓存。
 */

interface Deferred<T> { promise: Promise<T>; resolve: (value: T) => void; reject: (cause: unknown) => void }

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  let reject: (cause: unknown) => void = () => undefined;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

/** 每个键一条可手动结算的请求;同一个键重复请求时按发出顺序排队。 */
const pending = new Map<string, Deferred<string>[]>();
const loaders = new Map<string, () => Promise<string>>();

function loaderFor(key: string): () => Promise<string> {
  const existing = loaders.get(key);
  if (existing) return existing;
  const load = () => {
    const next = deferred<string>();
    pending.set(key, [...(pending.get(key) ?? []), next]);
    return next.promise;
  };
  loaders.set(key, load);
  return load;
}

async function settle(key: string, value: string, index = 0) {
  await act(async () => { pending.get(key)?.[index]?.resolve(value); await Promise.resolve(); });
}

async function fail(key: string, cause: unknown, index = 0) {
  await act(async () => { pending.get(key)?.[index]?.reject(cause); await Promise.resolve(); });
}

/** 每次渲染都记下钩子的输出(提交后记,渲染期不写外部变量)。 */
const frames: AsyncData<string>[] = [];
let latest: (AsyncData<string> & { refreshing: boolean }) | null = null;

function Probe({ name, cached = true, enabled = true }: { name: string; cached?: boolean; enabled?: boolean }) {
  const state = useAsyncData(loaderFor(name), enabled, { cacheKey: cached ? `/orders?q=${name}` : null });
  useEffect(() => { frames.push(state); latest = state; });
  return <div data-test-id="probe" data-value={state.data ?? ""} data-loading={String(state.loading)} data-refreshing={String(state.refreshing)} />;
}

/** 按资源路径取键的探针(详情页形态)。 */
function PathProbe({ path }: { path: string }) {
  const state = useAsyncData(loaderFor(path), true, { cacheKey: path });
  useEffect(() => { latest = state; });
  return null;
}

/**
 * 模拟 `platformRequest` 的在途 GET 合并:有未落地的请求时再调用拿到的是同一个 thenable,
 * 落地先离开合并表(登记在调用方的 then 之前),之后的调用才发新请求。
 */
const shared = { calls: [] as Deferred<string>[], open: null as Deferred<string> | null };
const loadShared = () => {
  if (!shared.open) {
    const next = deferred<string>();
    const leave = () => { if (shared.open === next) shared.open = null; };
    next.promise.then(leave, leave);
    shared.open = next;
    shared.calls.push(next);
  }
  return shared.open.promise;
};

function SharedProbe() {
  const state = useAsyncData(loadShared, true, { cacheKey: "/orders/shared" });
  useEffect(() => { latest = state; });
  return null;
}

async function settleShared(index: number, value: string) {
  await act(async () => { shared.calls[index]?.resolve(value); for (let tick = 0; tick < 6; tick += 1) await Promise.resolve(); });
}

let objectPending: Deferred<{ items: string[] }> | null = null;
let objectLatest: AsyncData<{ items: string[] }> | null = null;
const loadObject = () => {
  objectPending = deferred<{ items: string[] }>();
  return objectPending.promise;
};

function ObjectProbe() {
  const state = useAsyncData(loadObject, true, { cacheKey: "/orders/object" });
  useEffect(() => { objectLatest = state; });
  return null;
}

const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  clearAsyncDataCache();
  pending.clear();
  loaders.clear();
  frames.length = 0;
  latest = null;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(props: { name: string; cached?: boolean; enabled?: boolean }) {
  act(() => root.render(<Probe {...props} />));
}

function remount(props: { name: string; cached?: boolean }) {
  act(() => root.unmount());
  root = createRoot(container);
  frames.length = 0;
  render(props);
}

function current(): AsyncData<string> & { refreshing: boolean } {
  if (!latest) throw new Error("probe is not mounted");
  return latest;
}

describe("useAsyncData cache", () => {
  it("renders a cached value on the very first frame of a remount and revalidates", async () => {
    render({ name: "a" });
    expect(current()).toMatchObject({ data: null, loading: true, refreshing: false });
    await settle("a", "first");
    expect(current()).toMatchObject({ data: "first", loading: false, refreshing: false });

    remount({ name: "a" });
    // 首帧就是缓存,没有加载态。
    expect(frames[0]).toMatchObject({ data: "first", loading: false, refreshing: true });
    expect(pending.get("a")).toHaveLength(2);
    await settle("a", "second", 1);
    expect(current()).toMatchObject({ data: "second", loading: false, refreshing: false });
  });

  it("does not cache without a key", async () => {
    render({ name: "a", cached: false });
    await settle("a", "first");
    remount({ name: "a", cached: false });
    expect(frames[0]).toMatchObject({ data: null, loading: true, refreshing: false });
  });

  it("keeps the previous data while a new key loads", async () => {
    render({ name: "a" });
    await settle("a", "page-1");
    frames.length = 0;
    render({ name: "b" });
    expect(frames.every((frame) => frame.loading === false)).toBe(true);
    expect(current()).toMatchObject({ data: "page-1", loading: false, refreshing: true });
    await settle("b", "page-2");
    expect(current()).toMatchObject({ data: "page-2", refreshing: false });
  });

  it("shows the cached value of the new key when it has one", async () => {
    render({ name: "a" });
    await settle("a", "page-1");
    render({ name: "b" });
    await settle("b", "page-2");
    render({ name: "a" });
    expect(current()).toMatchObject({ data: "page-1", loading: false, refreshing: true });
  });

  it("ignores a slower response of a key that is no longer current", async () => {
    render({ name: "a" });
    render({ name: "b" });
    await settle("b", "fresh");
    await settle("a", "stale");
    expect(current().data).toBe("fresh");
    // 过期的响应也不进缓存。
    remount({ name: "a" });
    expect(frames[0]).toMatchObject({ data: null, loading: true });
  });

  it("updates the cache on reload", async () => {
    render({ name: "a" });
    await settle("a", "before");
    act(() => current().reload());
    expect(current()).toMatchObject({ data: "before", loading: true });
    await settle("a", "after", 1);
    remount({ name: "a" });
    expect(frames[0]).toMatchObject({ data: "after", refreshing: true });
  });

  it("still resets to loading when a gate reopens without a cached value", async () => {
    render({ name: "a", cached: false });
    await settle("a", "old");
    render({ name: "a", cached: false, enabled: false });
    render({ name: "a", cached: false, enabled: true });
    expect(current()).toMatchObject({ data: null, loading: true });
    await fail("a", new Error("boom"), 1);
  });
});

/** 缓存清空 / 失效时,挂着的钩子也要跟上(不重挂)。 */
describe("useAsyncData cache events", () => {
  it("forgets everything on clearAsyncDataCache (logout / identity switch)", async () => {
    render({ name: "a" });
    await settle("a", "someone-else");
    act(() => clearAsyncDataCache());
    remount({ name: "a" });
    expect(frames[0]).toMatchObject({ data: null, loading: true, refreshing: false });
  });

  // 换人时页面没有重挂:手里那份上一个人的数据当场丢掉,并按新身份重取。
  it("drops live data and refetches on clear without a remount", async () => {
    render({ name: "a" });
    await settle("a", "user-A");
    act(() => clearAsyncDataCache());
    expect(current()).toMatchObject({ data: null, loading: true });
    expect(pending.get("a")).toHaveLength(2);
    await settle("a", "user-B", 1);
    expect(current().data).toBe("user-B");
  });

  it("never lets a response started before the clear land in a live hook", async () => {
    render({ name: "a" });
    act(() => clearAsyncDataCache({ refetch: false }));
    await settle("a", "previous-user");
    expect(current()).toMatchObject({ data: null, loading: true });
    // 登出:只清不重取。
    expect(pending.get("a")).toHaveLength(1);
    remount({ name: "a" });
    expect(frames[0]).toMatchObject({ data: null, loading: true });
  });

  it("revalidates a live hook whose key matches an invalidated prefix", async () => {
    render({ name: "a" });
    await settle("a", "before-write");
    remount({ name: "a" });
    // 写之前发出的复查(第 2 条)还在路上,写操作完成后失效。
    act(() => invalidateAsyncData("/orders?q=a"));
    expect(current()).toMatchObject({ data: "before-write", refreshing: true, loading: false });
    // 重取排在被作废的那次落地之后(真实传输会把同路径 GET 合并到在途请求上)。
    expect(pending.get("a")).toHaveLength(2);
    await settle("a", "stale-read", 1);
    expect(current().data).toBe("before-write");
    expect(pending.get("a")).toHaveLength(3);
    await settle("a", "after-write", 2);
    expect(current()).toMatchObject({ data: "after-write", refreshing: false });
  });

  // 写操作后失效:写之前发出的复查既不画也不回写缓存。
  it("keeps a read started before an invalidation out of the cache", async () => {
    render({ name: "a" });
    await settle("a", "before-write");
    remount({ name: "a" });
    act(() => invalidateAsyncData("/orders?q=a"));
    await settle("a", "stale-read", 1);
    expect(hasAsyncData("/orders?q=a")).toBe(false);
    expect(current().data).toBe("before-write");
    await settle("a", "after-write", 2);
    remount({ name: "a" });
    expect(frames[0]).toMatchObject({ data: "after-write", refreshing: true });
  });

  // 写穿:命中缓存、复查在途时写回整份新数据,旧复查落地不能把画面和缓存盖回写之前。
  it("retires a revalidation in flight when the key is replaced", async () => {
    render({ name: "a" });
    await settle("a", "before-write");
    remount({ name: "a" });
    expect(current()).toMatchObject({ data: "before-write", refreshing: true });
    act(() => replaceAsyncData("/orders?q=a", "written"));
    expect(current()).toMatchObject({ data: "written", loading: false, refreshing: false });
    await settle("a", "stale-read", 1);
    expect(current().data).toBe("written");
    // 写穿不重取。
    expect(pending.get("a")).toHaveLength(2);
    remount({ name: "a" });
    expect(frames[0]).toMatchObject({ data: "written", refreshing: true });
  });

  it("lets another key's read land after a replace", async () => {
    render({ name: "a" });
    act(() => replaceAsyncData("/orders?q=other", "written"));
    await settle("a", "fresh");
    expect(current()).toMatchObject({ data: "fresh", loading: false });
    expect(hasAsyncData("/orders?q=a")).toBe(true);
  });

  // 调用方常在读取失败时顺手失效本资源:失败态不跟着重取,否则就是死循环。
  it("does not refetch a failed hook on invalidation", async () => {
    render({ name: "a" });
    await fail("a", Object.assign(new Error("gone"), { status: 404 }));
    act(() => invalidateAsyncData("/orders?q=a"));
    expect(current()).toMatchObject({ data: null, error: true });
    expect(pending.get("a")).toHaveLength(1);
  });

  it("ignores an invalidation for another prefix", async () => {
    render({ name: "a" });
    await settle("a", "kept");
    act(() => invalidateAsyncData("/notifications"));
    expect(current()).toMatchObject({ data: "kept", refreshing: false });
    expect(pending.get("a")).toHaveLength(1);
  });

  it("invalidates by prefix", async () => {
    render({ name: "a" });
    await settle("a", "cached");
    act(() => invalidateAsyncData("/orders?q=a"));
    remount({ name: "a" });
    expect(frames[0]).toMatchObject({ data: null, loading: true });
  });
});

/** 被作废的读取 HTTP 还在路上:同键的下一次读取不许并进同一个 GET(传输层会合并纯 GET)。 */
describe("useAsyncData with a merged transport GET", () => {
  beforeEach(() => {
    shared.calls.length = 0;
    shared.open = null;
  });

  // 写穿作废了在途复查,调用方又补一次 reload():补的读取若并进同一个在途 GET,写之前的回包会以新 epoch 落地。
  it("does not let a follow-up reload join the GET that a replace retired", async () => {
    const mountShared = () => {
      act(() => root.unmount());
      root = createRoot(container);
      act(() => root.render(<SharedProbe />));
    };
    mountShared();
    await settleShared(0, "before-write");
    mountShared();
    expect(current()).toMatchObject({ data: "before-write", refreshing: true });
    act(() => {
      replaceAsyncData("/orders/shared", "written");
      current().reload();
    });
    expect(shared.calls).toHaveLength(2);
    await settleShared(1, "pre-write-body");
    expect(current().data).toBe("written");
    expect(shared.calls).toHaveLength(3);
    await settleShared(2, "after-write");
    expect(current()).toMatchObject({ data: "after-write", loading: false, refreshing: false });
  });

  it("does not let the refetch after an invalidation join the retired GET", async () => {
    act(() => root.render(<SharedProbe />));
    await settleShared(0, "before-write");
    act(() => current().reload());
    act(() => invalidateAsyncData("/orders/shared"));
    expect(shared.calls).toHaveLength(2);
    await settleShared(1, "pre-write-body");
    expect(current().data).toBe("before-write");
    expect(hasAsyncData("/orders/shared")).toBe(false);
    await settleShared(2, "after-write");
    expect(current().data).toBe("after-write");
    expect(hasAsyncData("/orders/shared")).toBe(true);
  });
});

/** 复查失败、换资源、就地修改:缓存不能画出不该看的数据。 */
describe("useAsyncData cache safety", () => {
  it("drops data and cache when the revalidation says 403", async () => {
    render({ name: "a" });
    await settle("a", "visible");
    remount({ name: "a" });
    await fail("a", Object.assign(new Error("forbidden"), { status: 403 }), 1);
    expect(current()).toMatchObject({ data: null, error: true, refreshing: false });
    remount({ name: "a" });
    expect(frames[0]).toMatchObject({ data: null, loading: true });
  });

  it("keeps the table and the cache when the revalidation hits a 500", async () => {
    render({ name: "a" });
    await settle("a", "visible");
    remount({ name: "a" });
    await fail("a", Object.assign(new Error("boom"), { status: 500 }), 1);
    expect(current()).toMatchObject({ data: "visible", error: true, loading: false, refreshing: false });
    remount({ name: "a" });
    expect(frames[0]).toMatchObject({ data: "visible", refreshing: true });
  });

  it("goes back to loading when the resource path changes, not just the query", async () => {
    act(() => root.render(<PathProbe path="/orders/A" />));
    await settle("/orders/A", "exam-A");
    act(() => root.render(<PathProbe path="/orders/B" />));
    expect(current()).toMatchObject({ data: null, loading: true, refreshing: false });
  });

  it("stores a copy so in-place mutation cannot poison the cache", async () => {
    const remountObject = () => {
      act(() => root.unmount());
      root = createRoot(container);
      act(() => root.render(<ObjectProbe />));
    };
    act(() => root.render(<ObjectProbe />));
    await act(async () => { objectPending?.resolve({ items: ["x"] }); await Promise.resolve(); });
    // 首次加载拿到的那份。
    objectLatest?.data?.items.push("mutated-first-load");
    remountObject();
    expect(objectLatest?.data).toEqual({ items: ["x"] });
    // 命中缓存拿到的那份也不是缓存本体。
    objectLatest?.data?.items.push("mutated-cache-hit");
    remountObject();
    expect(objectLatest?.data).toEqual({ items: ["x"] });
  });
});
