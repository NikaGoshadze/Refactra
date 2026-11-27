import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { RunDetail } from "@/components/run-detail";
import { TopBar } from "@/components/top-bar";

export const metadata: Metadata = { title: "Run · Refactra" };
const RUN_ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

export default async function RunPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  if (!RUN_ID.test(runId)) notFound();
  return (
    <div className="workspace">
      <TopBar />
      <main>
        <RunDetail runId={runId.toLowerCase()} />
      </main>
      <footer><span>Refactra</span><span>Run details</span></footer>
    </div>
  );
}
