import type { Route } from "@playwright/test";

/**
 * 「设置 → 通知」的后端替身(契约见 EasyFrame `docs/NOTIFICATION_SETTINGS.md` §4)。
 *
 * 为什么是一个有状态的替身而不是几个常量:这一页**即点即存**,一次点击就是一个 PATCH,
 * 而 PATCH 的响应是「更新后的整个分组」,切页签之后还要再读一遍。若每次应答都从初始值重
 * 新拼一份,用例里刚改过的那一格会在下一次读取时自己弹回去——那时失败的是替身,不是页面。
 * 所以这里按分组保留**可变**的两份值(个人 / 平台),补丁累积生效。
 *
 * 两处契约细节,替身必须照做,否则用例会把错的当对的:
 * - `editable` 由后端算好,前端不推:「我的通知」里是 `!managed`,「平台配置」里恒为 `true`。
 * - 托管中的分组,成员改不动:`PATCH /preferences` 回 409 `{"code": "notification_group_managed"}`。
 *
 * 每个用例各建一份(`mockPlatform` 在 `beforeEach` 里调),状态不跨用例。
 */

type LocalizedText = { zh: string; en: string };
type Channels = Record<string, boolean | null>;
interface SceneFixture { key: string; title: LocalizedText; description: LocalizedText; channels: Channels }
interface GroupFixture { key: string; title: LocalizedText; description: LocalizedText; scenes: SceneFixture[] }

/** 一次渠道改动(两个 PATCH 共用);托管改动只带 `managed`。 */
interface SwitchChange { group: string; scene?: string; channel?: string; enabled?: boolean; managed?: boolean }

const CHANNELS = [{ key: "dingtalk", available: true }, { key: "in_app", available: true }];

/**
 * 一个可编辑分组(exam)+ 一个平台托管分组(ops)。
 *
 * `exam.reminder` 的钉钉格是 `null`:本场景不支持这个渠道,页面画破折号而不是一个永远
 * 关着的开关。场景 key 带点是正常的,`data-test-id` 里原样保留。
 */
function seed(): GroupFixture[] {
  return [
    {
      key: "exam", title: { zh: "考试", en: "Exams" }, description: { zh: "考试相关通知", en: "Exam notifications" },
      scenes: [
        { key: "exam.result_released", title: { zh: "成绩发布", en: "Results released" }, description: { zh: "成绩发布时通知", en: "When results are released" }, channels: { dingtalk: true, in_app: true } },
        { key: "exam.reminder", title: { zh: "开考提醒", en: "Exam reminder" }, description: { zh: "开考前提醒", en: "Before the exam starts" }, channels: { dingtalk: null, in_app: false } },
      ],
    },
    {
      key: "ops", title: { zh: "运维", en: "Operations" }, description: { zh: "系统服务通知", en: "System service notifications" },
      scenes: [{ key: "ops.upstream_down", title: { zh: "上游中断", en: "Upstream down" }, description: { zh: "上游不可用时通知", en: "When an upstream is unavailable" }, channels: { dingtalk: false, in_app: true } }],
    },
  ];
}

export interface NotificationSettingsMock {
  /** 命中通知设置的三条路由时自己应答并返回 `true`;其余路径返回 `false`,交回给调用方。 */
  handle(path: string, route: Route): Promise<boolean>;
}

export function createNotificationSettingsMock(): NotificationSettingsMock {
  // 个人值与平台值是**两份独立的数据**;`managed` 是分组属性,两个视图读同一份。
  const mine = seed();
  const policy = seed();
  const managed: Record<string, boolean> = { exam: false, ops: true };

  const find = (source: GroupFixture[], key: string) => source.find((group) => group.key === key) ?? source[0];
  const view = (group: GroupFixture, policyMode: boolean) => ({ ...group, managed: managed[group.key], editable: policyMode || !managed[group.key] });
  const listing = (source: GroupFixture[], policyMode: boolean) => source.map((group) => view(group, policyMode));
  const json = (route: Route, body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  const change = (route: Route) => JSON.parse(route.request().postData() ?? "{}") as SwitchChange;

  /** 补丁累积落在传进来的那一份上(个人或平台),下一次读取看得到。 */
  function applySwitch(source: GroupFixture[], patch: SwitchChange): GroupFixture {
    const group = find(source, patch.group);
    const scene = group.scenes.find((item) => item.key === patch.scene);
    if (scene && typeof patch.channel === "string") scene.channels[patch.channel] = patch.enabled === true;
    return group;
  }

  function handlePolicy(route: Route): Promise<unknown> {
    if (route.request().method() !== "PATCH") return json(route, { channels: CHANNELS, groups: listing(policy, true) });
    const patch = change(route);
    if (typeof patch.scene === "string") return json(route, view(applySwitch(policy, patch), true));
    managed[patch.group] = patch.managed === true;
    return json(route, view(find(policy, patch.group), true));
  }

  function handlePreference(route: Route): Promise<unknown> {
    const patch = change(route);
    // 分组在用户读页面这段时间里被改成了托管:成员的这次改动不该落库。
    if (managed[patch.group]) {
      return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { code: "notification_group_managed" } }) });
    }
    return json(route, view(applySwitch(mine, patch), false));
  }

  return {
    async handle(path, route) {
      // 分组已由后端按 gate 权限过滤,前端不重复判定;`canManage` 决定要不要画「平台配置」页签。
      if (path === "/api/v1/notification-settings") await json(route, { canManage: true, channels: CHANNELS, groups: listing(mine, false) });
      else if (path === "/api/v1/notification-settings/preferences") await handlePreference(route);
      else if (path === "/api/v1/notification-settings/policy") await handlePolicy(route);
      else return false;
      return true;
    },
  };
}
