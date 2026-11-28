import type { Metadata } from "next";
import { RunConsole } from "@/components/run-console";
import { TopBar } from "@/components/top-bar";

export const metadata: Metadata = { title: "Mock run · Refactra development" };

// Development only: not linked from the site, and the API refuses mock runs unless mock mode is
// on, which production mode never allows.
export default function MockRunPage() {
  return (
    <div className="workspace">
      <TopBar />
      <main>
        <header className="page-heading">
          <p className="eyebrow">DEVELOPMENT ONLY</p>
          <h1>Mock run</h1>
          <p className="intro-copy">Sends a task through PostgreSQL to the mock worker without GitHub or a model.</p>
        </header>
        <RunConsole />
      </main>
    </div>
  );
}
