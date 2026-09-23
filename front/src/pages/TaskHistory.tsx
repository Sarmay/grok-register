import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Clock3, History, RefreshCw, Search, Trash2, Users } from "lucide-react";
import { LiveLogBoard } from "@/components/LiveLogBoard";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Input,
  PageHeader,
  PaginationBar,
  Toast,
  buttonVariants,
} from "@/components/ui";
import { api, type LogItem, type TaskRun } from "@/lib/api";
import { cn } from "@/lib/utils";

const KIND = "registration" as const;

function formatWhen(value: number | null | undefined) {
  return value ? new Date(value * 1000).toLocaleString() : "时间未知";
}

function formatDuration(start: number | null | undefined, end: number | null | undefined) {
  if (!start) return "";
  const seconds = Math.max(0, Math.round(((end || Date.now() / 1000) - start)));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function statusLabel(status: string, active?: boolean) {
  if (active || status === "running") return "运行中";
  const labels: Record<string, string> = {
    finished: "已完成",
    stopped: "提前结束",
    failed: "异常结束",
    interrupted: "服务重启中断",
  };
  return labels[status] || status || "已完成";
}

function statusVariant(status: string, active?: boolean) {
  if (active || status === "running") return "warning" as const;
  if (status === "failed" || status === "interrupted") return "destructive" as const;
  if (status === "stopped") return "secondary" as const;
  return "success" as const;
}

function accountsLink(runId: string) {
  return `/accounts?batch_id=${encodeURIComponent(runId)}`;
}

function useToast() {
  const [toast, setToast] = useState<{ message: string; tone?: "default" | "success" | "error" }>({ message: "" });
  const show = (message: string, tone: "default" | "success" | "error" = "default") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: "" }), 2200);
  };
  return { toast, show };
}

export function TaskHistoryPage() {
  const [items, setItems] = useState<TaskRun[]>([]);
  const [total, setTotal] = useState(0);
  const [activeRunId, setActiveRunId] = useState("");
  const [query, setQuery] = useState("");
  const [applied, setApplied] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const { toast, show } = useToast();

  const load = async (targetPage = page, targetPageSize = pageSize, keyword = applied) => {
    setLoading(true);
    try {
      const result = await api.taskRuns({
        kind: KIND,
        q: keyword || undefined,
        limit: targetPageSize,
        offset: (targetPage - 1) * targetPageSize,
      });
      setItems(result.items || []);
      setTotal(result.total || 0);
      setActiveRunId(result.active_run_id || "");
      setPage(targetPage);
      setPageSize(targetPageSize);
      setError("");
    } catch (reason: any) {
      setError(reason.message || "任务历史加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(1, pageSize, applied);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applied]);

  useEffect(() => {
    if (!activeRunId) return;
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRunId, page, pageSize, applied]);

  const remove = async (run: TaskRun) => {
    if (!window.confirm(`删除批次 ${run.run_id} 的任务日志？账号记录不受影响。`)) return;
    try {
      await api.deleteTaskRun(KIND, run.run_id);
      show("已删除任务日志", "success");
      await load();
    } catch (reason: any) {
      show(reason.message || "删除失败", "error");
    }
  };

  const clearAll = async () => {
    if (!window.confirm("清空全部注册批次的任务日志？账号记录不受影响，运行中的任务会保留。")) return;
    try {
      const result = await api.clearTaskRuns(KIND);
      show(`已清空 ${result.deleted} 个批次的日志`, "success");
      await load(1);
    } catch (reason: any) {
      show(reason.message || "清空失败", "error");
    }
  };

  return (
    <div className="space-y-5 sm:space-y-6">
      <PageHeader
        title="任务历史"
        description="每次注册任务按批次号保存完整日志和结果统计，服务重启后仍可回看。"
        actions={
          <>
            <Button variant="outline" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={cn("h-4 w-4", loading ? "animate-spin" : "")} aria-hidden="true" />
              刷新
            </Button>
            <Button variant="outline" className="text-red-700" disabled={!items.length} onClick={() => void clearAll()}>
              <Trash2 className="h-4 w-4" aria-hidden="true" />
              清空日志
            </Button>
          </>
        }
      />

      {error ? <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">{error}</div> : null}

      <Card className="p-4">
        <form
          className="flex flex-col gap-3 md:flex-row md:items-center"
          onSubmit={(event) => {
            event.preventDefault();
            setApplied(query.trim());
          }}
        >
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-3 top-3.5 h-4 w-4 text-slate-400" aria-hidden="true" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索批次号或这批任务里的邮箱"
              className="pl-9"
            />
          </div>
          <Button type="submit" disabled={loading}>查询</Button>
        </form>
      </Card>

      {items.length ? (
        <section className="grid gap-3 xl:grid-cols-2">
          {items.map((run) => {
            const active = run.run_id === activeRunId;
            return (
              <Card key={run.run_id} className="p-4 sm:p-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2 text-sm font-semibold text-slate-950">
                      <History className="h-4 w-4 shrink-0 text-sky-600" aria-hidden="true" />
                      <span className="break-all font-mono text-xs sm:text-sm">{run.run_id}</span>
                      <Badge variant={statusVariant(run.status, active)}>{statusLabel(run.status, active)}</Badge>
                    </div>
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                      <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" aria-hidden="true" />{formatWhen(run.started_at)}</span>
                      {formatDuration(run.started_at, run.finished_at) ? <span>耗时 {formatDuration(run.started_at, run.finished_at)}</span> : null}
                      {run.summary?.workers ? <span>并发 {run.summary.workers}</span> : null}
                    </div>
                    <div className="mt-3 flex flex-wrap gap-1.5">
                      <Badge variant="secondary">目标 {run.summary?.target_count ?? run.counts.total}</Badge>
                      <Badge variant="success">成功 {run.counts.success}</Badge>
                      {run.counts.failure ? <Badge variant="destructive">失败 {run.counts.failure}</Badge> : null}
                      {run.counts.cancelled ? <Badge variant="secondary">取消 {run.counts.cancelled}</Badge> : null}
                      <Badge variant={run.log_count ? "outline" : "secondary"}>{run.log_count ? `日志 ${run.log_count} 行` : "无日志"}</Badge>
                    </div>
                    {run.summary?.last_error ? (
                      <p className="mt-2 break-words text-xs leading-5 text-red-700">最近错误：{run.summary.last_error}</p>
                    ) : null}
                  </div>
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-9 w-9 shrink-0 text-red-700"
                    disabled={active}
                    title={active ? "任务运行中，结束后才能删除" : "删除这个批次的日志"}
                    onClick={() => void remove(run)}
                  >
                    <Trash2 className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </div>
                <div className="mt-4 flex flex-wrap gap-2 border-t border-slate-100 pt-3">
                  <Link
                    to={`/registration/history/${encodeURIComponent(run.run_id)}`}
                    className="inline-flex min-h-9 flex-1 items-center justify-center rounded-lg bg-slate-900 px-3 text-xs font-medium text-white hover:bg-slate-800"
                  >
                    查看日志
                  </Link>
                  <Link to={accountsLink(run.run_id)} className={cn(buttonVariants({ variant: "outline", size: "sm" }), "inline-flex min-h-9 items-center gap-1.5")}>
                    <Users className="h-3.5 w-3.5" aria-hidden="true" />
                    查看账号
                  </Link>
                </div>
              </Card>
            );
          })}
        </section>
      ) : (
        <Card className="p-4">
          <EmptyState
            title={loading ? "正在加载…" : applied ? "没有匹配的批次" : "暂无任务历史"}
            description={applied ? "换个批次号或邮箱试试。" : "启动一次注册任务后，这里会按批次保存日志和结果。"}
          />
        </Card>
      )}
      {total > 0 ? (
        <Card className="overflow-hidden">
          <PaginationBar
            page={page}
            pageSize={pageSize}
            total={total}
            loading={loading}
            onPageChange={(next) => void load(next)}
            onPageSizeChange={(size) => void load(1, size)}
          />
        </Card>
      ) : null}
      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}

export function TaskHistoryDetailPage() {
  const { runId = "" } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [run, setRun] = useState<TaskRun | null>(null);
  const [missing, setMissing] = useState(false);
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const lastSeqRef = useRef(0);
  const { toast, show } = useToast();
  const initialQuery = searchParams.get("q") || "";

  const refresh = async (full: boolean) => {
    try {
      const detail = await api.taskRun(KIND, runId);
      setRun(detail.run);
      setMissing(false);
      const after = full ? 0 : lastSeqRef.current;
      const result = await api.taskRunLogs(KIND, runId, after, 5000);
      const fresh = result.logs || [];
      if (fresh.length) lastSeqRef.current = fresh[fresh.length - 1].id;
      setLogs((previous) => (full ? fresh : [...previous, ...fresh]));
    } catch (reason: any) {
      if (String(reason?.message || "").includes("不存在")) setMissing(true);
      else show(reason.message || "加载失败", "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    lastSeqRef.current = 0;
    setLogs([]);
    setLoading(true);
    void refresh(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => {
    if (!run?.active) return;
    const timer = window.setInterval(() => void refresh(false), 2000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.active, runId]);

  const remove = async () => {
    if (!run || !window.confirm(`删除批次 ${run.run_id} 的任务日志？账号记录不受影响。`)) return;
    try {
      await api.deleteTaskRun(KIND, run.run_id);
      navigate("/registration/history", { replace: true });
    } catch (reason: any) {
      show(reason.message || "删除失败", "error");
    }
  };

  if (missing) {
    return (
      <div className="space-y-5">
        <Link to="/registration/history" className="text-sm text-sky-700 hover:underline">← 返回任务历史</Link>
        <Card className="p-5">
          <EmptyState title="没有这个批次" description="记录可能已被删除，或已按保留策略清理。" />
        </Card>
      </div>
    );
  }

  const summary = run?.summary || {};
  const stats: Array<[string, string | number, string]> = [
    ["目标账号", summary.target_count ?? run?.counts.total ?? 0, "text-slate-950"],
    ["成功", run?.counts.success ?? 0, "text-emerald-700"],
    ["失败", run?.counts.failure ?? 0, "text-red-700"],
    ["耗时", formatDuration(run?.started_at, run?.finished_at) || "—", "text-slate-950"],
  ];

  return (
    <div className="space-y-5 sm:space-y-6">
      <Link to="/registration/history" className="inline-flex items-center gap-1 text-sm text-sky-700 hover:underline">← 返回任务历史</Link>
      <PageHeader
        title="任务日志"
        description={run ? `${formatWhen(run.started_at)} 开始 · 批次 ${run.run_id}` : runId}
        actions={
          <>
            {run ? <Badge variant={statusVariant(run.status, run.active)}>{statusLabel(run.status, run.active)}</Badge> : null}
            <Link to={accountsLink(runId)} className={cn(buttonVariants({ variant: "outline", size: "sm" }), "inline-flex items-center gap-1.5")}>
              <Users className="h-3.5 w-3.5" aria-hidden="true" />
              查看账号
            </Link>
            <Button size="sm" variant="outline" className="text-red-700" disabled={!run || run.active} onClick={() => void remove()}>
              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
              删除
            </Button>
          </>
        }
      />
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {stats.map(([label, value, tone]) => (
          <Card key={label} className="p-4">
            <div className="text-xs text-slate-500">{label}</div>
            <div className={`mt-2 text-2xl font-semibold tabular-nums ${tone}`}>{loading ? "…" : value}</div>
          </Card>
        ))}
      </section>
      {summary.last_error ? (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm leading-6 text-red-800">最近错误：{summary.last_error}</div>
      ) : null}
      {!loading && !logs.length && !run?.active ? (
        <Card className="p-4">
          <EmptyState title="这个批次没有保存日志" description="它由旧版本运行，或日志已按保留策略清理。账号结果仍可在账号列表按批次查看。" />
        </Card>
      ) : (
        <LiveLogBoard
          logs={logs}
          running={!!run?.active}
          title="任务日志"
          description="这个批次从启动到结束的全部日志，和运行监控页看到的一致。"
          ariaLabel="任务日志"
          emptyIdleHint="没有日志。"
          emptyRunningHint="任务运行中，正在读取日志…"
          statusRunningLabel="任务运行中，日志持续写入"
          statusIdleLabel={run ? statusLabel(run.status, run.active) : "已结束"}
          initialQuery={initialQuery}
          onToast={show}
        />
      )}
      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}
