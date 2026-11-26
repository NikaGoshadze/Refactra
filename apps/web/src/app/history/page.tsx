import type { Metadata } from "next";
import { RunHistory } from "@/components/run-history";
import { TopBar } from "@/components/top-bar";

export const metadata: Metadata = { title: "History · Refactra" };

export default function HistoryPage() {
  return (
    <div className="workspace">
      <TopBar />
      <main>
        <section className="intro">
          <p className="eyebrow">YOUR HISTORY</p>
          <h1>Your refactoring runs</h1>
        </section>
        <RunHistory />
      </main>
      <footer><span>Refactra</span><span>Run history</span></footer>
    </div>
  );
}
