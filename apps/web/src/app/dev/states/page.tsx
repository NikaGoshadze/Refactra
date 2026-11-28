import { DiffView } from "@/components/diff-view";
import { notFound } from "next/navigation";

const review = [
  "--- a/pizza_app/main.py",
  "+++ b/pizza_app/main.py",
  "@@ -104,4 +104,4 @@ def run_chatbot():",
  " if is_summary_command(user_input):",
  "-    print(memory.moving_summary_buffer)",
  "-    print(\"Done.\")",
  "+    for line in summary_lines(memory.moving_summary_buffer):",
  "+        print(line)",
  "     continue",
  "--- /dev/null",
  "+++ b/pizza_app/summary.py",
  "@@ -0,0 +1,4 @@",
  "+def summary_lines(summary: str) -> list[str]:",
  "+    if not summary:",
  "+        return [\"No conversation summary yet.\"]",
  "+    return [summary]",
  "",
].join("\n");

const asset = "Replace binary file docs/logo.png\nBefore SHA-256 " + "a".repeat(64) + "\nAfter SHA-256 " + "b".repeat(64);

export default function StatesPage() {
  if (process.env.NODE_ENV !== "development") notFound();

  return <main className="workspace">
    <header className="page-heading">
      <h1>Screen states</h1>
      <p className="intro-copy">Sample data for reviewing the interface locally.</p>
    </header>
    <section className="repository-card" aria-labelledby="diff-preview">
      <h2 id="diff-preview">Candidate diff</h2>
      <DiffView diff={review} />
    </section>
    <section className="repository-card" aria-labelledby="asset-preview">
      <h2 id="asset-preview">Binary asset replacement</h2>
      <DiffView diff={asset} />
    </section>
    <section className="repository-card" aria-labelledby="empty-preview">
      <h2 id="empty-preview">No diff</h2>
      <DiffView diff="" />
    </section>
  </main>;
}
