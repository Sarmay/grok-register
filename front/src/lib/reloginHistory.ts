import { api, type ReloginItem, type TaskRun } from "@/lib/api";
import { dropLegacyHistory, readLegacyHistory } from "@/lib/legacyHistory";

export type ReloginHistoryEntry = {
  run_id: string;
  started_at: number | null;
  finished_at: number | null;
  status: string;
  log_count: number;
  total_count: number;
  success_count: number;
  failed_count: number;
  items: ReloginItem[];
};

const LEGACY_DB = "grok-register-relogin-history";
const LEGACY_STORE = "relogin-history";
let legacyMigrated = false;

function toEntry(run: TaskRun): ReloginHistoryEntry {
  const summary = run.summary || {};
  const items = Array.isArray(summary.items) ? (summary.items as ReloginItem[]) : [];
  return {
    run_id: run.run_id,
    started_at: run.started_at,
    finished_at: run.finished_at,
    status: run.status,
    log_count: run.log_count,
    total_count: Number(summary.total_count ?? run.counts?.total ?? items.length),
    success_count: Number(summary.success_count ?? run.counts?.success ?? 0),
    failed_count: Number(summary.failed_count ?? run.counts?.failure ?? 0),
    items: items.map((item) => ({ ...item, error: String(item.error || "") })),
  };
}

/** 旧版本存在浏览器里的报告只搬一次；搬成功才删本地库。 */
async function migrateLegacyHistory() {
  if (legacyMigrated) return;
  legacyMigrated = true;
  const rows = await readLegacyHistory<Partial<ReloginHistoryEntry>>(LEGACY_DB, LEGACY_STORE);
  const entries = rows.filter((row) => row && typeof row.run_id === "string");
  if (!entries.length) {
    dropLegacyHistory(LEGACY_DB);
    return;
  }
  try {
    await api.importTaskRuns(
      "relogin",
      entries.map((row) => ({
        run_id: row.run_id,
        started_at: row.started_at ?? row.finished_at ?? null,
        finished_at: row.finished_at ?? null,
        summary: {
          total_count: row.total_count || 0,
          success_count: row.success_count || 0,
          failed_count: row.failed_count || 0,
          items: row.items || [],
        },
      }))
    );
    dropLegacyHistory(LEGACY_DB);
  } catch {
    legacyMigrated = false;
  }
}

/** 按开始时间倒序返回服务端保存的全部重新登录报告。 */
export async function loadReloginHistory(): Promise<ReloginHistoryEntry[]> {
  await migrateLegacyHistory();
  const result = await api.taskRuns({ kind: "relogin", limit: 500 });
  return (result.items || []).map(toEntry);
}

export async function removeReloginHistory(runId: string): Promise<ReloginHistoryEntry[]> {
  await api.deleteTaskRun("relogin", runId);
  return loadReloginHistory();
}

export async function clearReloginHistory(): Promise<ReloginHistoryEntry[]> {
  await api.clearTaskRuns("relogin");
  return loadReloginHistory();
}
