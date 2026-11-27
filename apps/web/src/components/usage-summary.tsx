"use client";

import { useEffect, useState } from "react";
import { request } from "@/lib/api";

type Usage = {
  runs_today: number; daily_run_limit: number; month_cost_usd: string; monthly_limit_usd: string;
};

/** The signed-in user's remaining allowance; it stays hidden if usage cannot be loaded. */
export function UsageSummary() {
  const [usage, setUsage] = useState<Usage | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    request<Usage>("usage", { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(7000)]) })
      .then((value) => { if (!controller.signal.aborted) setUsage(value); })
      .catch(() => { /* The allowance is informational; submissions still enforce the limits. */ });
    return () => controller.abort();
  }, []);

  if (!usage) return null;
  return <span className="usage-summary">
    Today: {usage.runs_today} of {usage.daily_run_limit} runs · This month: ${usage.month_cost_usd} of ${usage.monthly_limit_usd}
  </span>;
}
