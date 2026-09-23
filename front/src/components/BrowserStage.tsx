import { useEffect, useRef, useState } from "react";
import { Maximize2, Minimize2, Monitor } from "lucide-react";
import { api, type BrowserViewStatus } from "@/lib/api";
import { Button, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui";
import { cn } from "@/lib/utils";

export function BrowserStage({ workers = 1, className }: { workers?: number; className?: string }) {
  const [status, setStatus] = useState<BrowserViewStatus | null>(null);
  const [src, setSrc] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [frameError, setFrameError] = useState("");
  const urlRef = useRef("");

  useEffect(() => {
    return () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const next = await api.browserView();
        if (alive) setStatus(next);
      } catch (error) {
        if (alive) {
          setStatus(null);
          setFrameError(error instanceof Error ? error.message : "画面状态获取失败");
        }
      }
    };
    void tick();
    const timer = window.setInterval(tick, 3000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!status?.enabled) {
      setSrc("");
      return;
    }
    let alive = true;
    let pending = false;
    const tick = async () => {
      if (pending) return;
      pending = true;
      try {
        const blob = await api.browserViewFrame();
        if (!alive) return;
        setFrameError("");
        if (!blob) return;
        const next = URL.createObjectURL(blob);
        const previous = urlRef.current;
        urlRef.current = next;
        setSrc(next);
        if (previous) URL.revokeObjectURL(previous);
      } catch (error) {
        if (alive) setFrameError(error instanceof Error ? error.message : "画面获取失败");
      } finally {
        pending = false;
      }
    };
    void tick();
    const timer = window.setInterval(tick, 800);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [status?.enabled]);

  const live = !!src && !!status?.capturing && (status.age_seconds == null || status.age_seconds < 5);
  const placeholder = frameError || status?.reason || status?.error || "正在连接虚拟屏幕…";

  return (
    // 放大时在并排布局里独占整行，并取消吸顶，避免盖住下面的日志
    <Card className={cn("overflow-hidden", expanded ? "xl:col-span-2" : className)}>
      <CardHeader className="border-b border-slate-100">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <CardTitle className="flex flex-wrap items-center gap-2">
              <Monitor className="h-4 w-4 text-slate-600" />
              浏览器画面
              <span
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium",
                  live ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-600"
                )}
              >
                <span className={cn("h-1.5 w-1.5 rounded-full", live ? "animate-pulse bg-emerald-500" : "bg-slate-400")} />
                {live ? "实时" : "未连接"}
              </span>
            </CardTitle>
            <CardDescription className="mt-1">
              只读查看容器虚拟屏幕上的有头浏览器，操作仍由注册流程执行。
              {workers > 1 ? " 多个并发窗口会叠在同一块屏幕上。" : ""}
            </CardDescription>
          </div>
          <Button variant="outline" size="sm" onClick={() => setExpanded((value) => !value)} disabled={!src}>
            {expanded ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
            {expanded ? "收起" : "放大"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-3 sm:p-4">
        <div
          className={cn(
            "relative flex items-center justify-center overflow-hidden rounded-xl bg-slate-950",
            expanded ? "h-[70vh]" : "aspect-video"
          )}
        >
          {src ? (
            <img src={src} alt="当前浏览器画面" className="h-full w-full object-contain" />
          ) : (
            <p className="max-w-md px-6 text-center text-sm leading-6 text-slate-400">{placeholder}</p>
          )}
        </div>
        {src && (status?.error || frameError) ? (
          <p className="mt-2 text-xs leading-5 text-rose-600">{frameError || status?.error}</p>
        ) : null}
      </CardContent>
    </Card>
  );
}
