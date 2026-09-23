import { api, type SsoCheckItem, type TaskRun } from "@/lib/api";
import { dropLegacyHistory, readLegacyHistory } from "@/lib/legacyHistory";

export type SsoCheckHistoryEntry = {
  run_id: string;
  started_at: number | null;
  finished_at: number | null;
  status: string;
  log_count: number;
  total_count: number;
  clean_count: number;
  flagged_count: number;
  unknown_count: number;
  failed_count: number;
  items: SsoCheckItem[];
};

const LEGACY_DB = "grok-register-sso-check-history";
const LEGACY_STORE = "sso-check-history";
let legacyMigrated = false;

function toEntry(run: TaskRun): SsoCheckHistoryEntry {
  const summary = run.summary || {};
  const items = Array.isArray(summary.items) ? (summary.items as SsoCheckItem[]) : [];
  return {
    run_id: run.run_id,
    started_at: run.started_at,
    finished_at: run.finished_at,
    status: run.status,
    log_count: run.log_count,
    total_count: Number(summary.total_count ?? items.length),
    clean_count: Number(summary.clean_count ?? 0),
    flagged_count: Number(summary.flagged_count ?? 0),
    unknown_count: Number(summary.unknown_count ?? 0),
    failed_count: Number(summary.failed_count ?? 0),
    items: items.map((item) => ({ ...item, error: String(item.error || "") })),
  };
}

async function migrateLegacyHistory() {
  if (legacyMigrated) return;
  legacyMigrated = true;
  const rows = await readLegacyHistory<Partial<SsoCheckHistoryEntry>>(LEGACY_DB, LEGACY_STORE);
  const entries = rows.filter((row) => row && typeof row.run_id === "string");
  if (!entries.length) {
    dropLegacyHistory(LEGACY_DB);
    return;
  }
  try {
    await api.importTaskRuns(
      "sso_check",
      entries.map((row) => ({
        run_id: row.run_id,
        started_at: row.started_at ?? row.finished_at ?? null,
        finished_at: row.finished_at ?? null,
        summary: {
          total_count: row.total_count || 0,
          clean_count: row.clean_count || 0,
          flagged_count: row.flagged_count || 0,
          unknown_count: row.unknown_count || 0,
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

export async function loadSsoCheckHistory(): Promise<SsoCheckHistoryEntry[]> {
  await migrateLegacyHistory();
  const result = await api.taskRuns({ kind: "sso_check", limit: 500 });
  return (result.items || []).map(toEntry);
}

export async function removeSsoCheckHistory(runId: string) {
  await api.deleteTaskRun("sso_check", runId);
  return loadSsoCheckHistory();
}

export async function clearSsoCheckHistory() {
  await api.clearTaskRuns("sso_check");
  return loadSsoCheckHistory();
}
