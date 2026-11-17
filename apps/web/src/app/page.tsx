import { RunConsole } from "@/components/run-console";
import { RepositoryConnection } from "@/components/repository-connection";
import Link from "next/link";

export default function Home() {
  return (
    <div className="workspace">
      <header className="topbar">
        <Link className="brand" href="/" aria-label="Refactra home">
          <span className="brand-mark" aria-hidden="true">r/</span>Refactra
        </Link>
        <span className="environment"><span className="status-dot" />Local foundation <span>0.1</span></span>
      </header>
      <main>
        <RepositoryConnection>
        <section className="intro">
          <p className="eyebrow">YOUR REPOSITORY. YOUR APPROVAL.</p>
          <h1>A clearer path<br />from task to change.</h1>
          <p className="intro-copy">Refactra is being built to analyze, plan and refactor code through two human approvals. This workspace demonstrates verified repository access and a durable mock task queue.</p>
        </section>
        </RepositoryConnection>
        <RunConsole />
        <section className="roadmap" aria-labelledby="roadmap-title">
          <div><p className="eyebrow">THE INTENDED WORKFLOW</p><h2 id="roadmap-title">Small steps. Explicit decisions.</h2></div>
          <ol className="workflow">
            <li><span>01</span><strong>Analyze & plan</strong><small>Understand the repository</small></li>
            <li><span>02</span><strong>Approve the plan</strong><small>Human checkpoint</small></li>
            <li><span>03</span><strong>Edit, test & review</strong><small>Restricted execution</small></li>
            <li><span>04</span><strong>Approve a draft PR</strong><small>Human checkpoint</small></li>
          </ol>
          <p className="roadmap-note">Planned capabilities — these stages are not executable in this foundation build.</p>
        </section>
      </main>
      <footer><span>Built deliberately, one verified boundary at a time.</span><span>No live AI · No GitHub writes</span></footer>
    </div>
  );
}
